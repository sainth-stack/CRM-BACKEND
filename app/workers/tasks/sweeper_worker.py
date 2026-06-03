from app.db import models
from app.core.logging_config import logger
from app.workers.config.celery_app import celery_app
from app.workers.utils import db_session
import datetime
from datetime import UTC
from app.core.config import settings

# ── Stranded-dispatch tuning ─────────────────────────────────────────────────
# A QUEUED draft whose scheduled_at is this many minutes in the past is
# considered stranded (the Celery task was lost — Redis restart, worker killed
# before ack, etc.).  Set higher than the longest normal task-start delay
# (~1 s in practice) but low enough to catch a missed delivery window quickly.
_STALE_GRACE_MINUTES = 30

# Drafts stranded longer than this many days are too stale to auto-recover —
# mark them FAILED so the user can review and decide what to do.
_MAX_STALE_DAYS = 3


@celery_app.task
def sweep_stuck_campaigns_task():
    """
    Resurrection Protocol: Recovers ephemeral operations lost to server restarts or unexpected infrastructure failures.
    Scans the database for campaigns in active but non-terminal states and re-injects them into the appropriate Celery task queue.
    """
    logger.info("[SENTINEL] Sweeping for ghosted background operations...")
    with db_session() as db:
        try:
            threshold = datetime.datetime.now(UTC) - datetime.timedelta(minutes=settings.SWEEP_STUCK_MINUTES)
            stuck_campaigns = db.query(models.Campaign).filter(
                models.Campaign.status.in_([
                    models.CampaignStatus.PENDING,
                    models.CampaignStatus.INPUT_VALIDATED,
                    models.CampaignStatus.RESEARCHING_USER_COMPANY,
                    models.CampaignStatus.STAGE_1_CSV_TRIMMED,
                    models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE,
                    models.CampaignStatus.STAGE_3_ICP_FILTERED,
                    models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE,
                    models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED,
                    models.CampaignStatus.STAGE_6_DRAFTING_COMPLETE,
                    "INTERVENTION_NEEDED"
                ]),
                (models.Campaign.last_heartbeat == None) | (models.Campaign.last_heartbeat < threshold)
            ).all()

            count = len(stuck_campaigns)
            if count == 0:
                logger.debug("[SENTINEL] No ghosted operations found or all active leases are current.")
                return

            logger.info(f"[SENTINEL] Discovered {count} dropped operations with expired leases. Initializing V3 Resurrection...")

            from app.workers.tasks.intel_worker import process_csv_worker

            for campaign in stuck_campaigns:
                owner = campaign.owner
                if owner and owner.is_demo and owner.demo_expires_at:
                    if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                        logger.info(f"[RECOVERY] Skipping Campaign {campaign.id} due to expired trial.")
                        continue

                logger.info(f"[RECOVERY] Resurrecting Campaign {campaign.id} from Stage: {campaign.status.name}")
                campaign.last_heartbeat = None
                campaign.locked_by = None
                db.commit()

                # CRITICAL: also clear any stranded Redis stage-locks. Resetting the
                # DB lease alone is not enough — a crashed worker leaves the Redis
                # lock held for its full TTL, and process_state_machine's re-trigger
                # would silently no-op on it. Clearing them here lets the campaign
                # actually advance.
                from app.core.security import release_lock
                for stage in ("stage3", "stage4", "stage5"):
                    release_lock(f"campaign_{stage}:{campaign.id}")

                from app.services.campaign_service import campaign_service
                campaign_service.process_state_machine(db, campaign.id)

        except Exception as e:
            logger.error(f"[SENTINEL] Critical error during Resurrection Protocol sweep: {e}", exc_info=True)


@celery_app.task
def sweep_stranded_dispatches_task():
    """
    Dispatch Recovery Sweep — runs every 10 minutes via Celery Beat.

    Finds EmailDraft records that are stuck in dispatch_state='QUEUED' with a
    past-due scheduled_at.  This happens when:
      • The Celery worker was down when the eta fired (Redis intact).
        In that case the task will re-fire when the worker restarts and the
        window-guard in send_draft_worker will already handle it.  The sweeper
        acts as a safety-net for the window between "worker up" and "task re-fired".
      • Redis itself crashed — the Celery task was permanently lost from the
        queue.  The draft remains QUEUED in the DB indefinitely.  The sweeper
        is the *only* recovery mechanism for this scenario.

    Recovery logic:
      • Draft stranded < _MAX_STALE_DAYS  → re-queue to next delivery window.
      • Draft stranded ≥ _MAX_STALE_DAYS  → mark FAILED so the user can review.
    """
    import pytz
    from app.core.scheduler import (
        resolve_prospect_timezone,
        calculate_next_send_slot,
        format_scheduled_display,
    )
    from app.workers.tasks.outbound_worker import send_draft_worker

    logger.info("[DISPATCH-SWEEP] Scanning for stranded scheduled dispatches...")

    with db_session() as db:
        try:
            now_utc = datetime.datetime.now(UTC)
            # Grace cutoff: anything scheduled_at before this is treated as stranded
            grace_cutoff = (
                now_utc - datetime.timedelta(minutes=_STALE_GRACE_MINUTES)
            ).replace(tzinfo=None)
            # Max-stale cutoff: beyond this, auto-recovery is too risky
            max_stale_cutoff = (
                now_utc - datetime.timedelta(days=_MAX_STALE_DAYS)
            ).replace(tzinfo=None)

            stranded_drafts = (
                db.query(models.EmailDraft)
                .filter(
                    models.EmailDraft.dispatch_state == "QUEUED",
                    models.EmailDraft.scheduled_at.isnot(None),
                    models.EmailDraft.scheduled_at < grace_cutoff,
                    models.EmailDraft.status != "SENT",
                )
                .all()
            )

            if not stranded_drafts:
                logger.debug("[DISPATCH-SWEEP] No stranded dispatches found.")
                return

            logger.warning(
                f"[DISPATCH-SWEEP] Found {len(stranded_drafts)} stranded draft(s). "
                "Initiating recovery..."
            )

            recovered_count = 0
            expired_count = 0
            error_count = 0

            for draft in stranded_drafts:
                try:
                    dm = draft.dm
                    campaign = draft.campaign

                    if not dm or not campaign:
                        logger.warning(
                            f"[DISPATCH-SWEEP] Draft {draft.id} has no DM or campaign — "
                            "marking FAILED."
                        )
                        draft.dispatch_state = "FAILED"
                        draft.dispatch_error = "Stranded: DM or campaign missing during recovery."
                        db.commit()
                        expired_count += 1
                        continue

                    # ── Too stale — don't auto-send, surface to user ────────
                    if draft.scheduled_at < max_stale_cutoff:
                        draft.dispatch_state = "FAILED"
                        draft.dispatch_error = (
                            f"Stranded for more than {_MAX_STALE_DAYS} days "
                            f"(originally scheduled {draft.scheduled_at}). "
                            "Manual review required."
                        )
                        db.commit()
                        logger.error(
                            f"[DISPATCH-SWEEP] Draft {draft.id} ({dm.name}) stranded "
                            f"since {draft.scheduled_at} — too stale to auto-recover, "
                            "marked FAILED."
                        )
                        expired_count += 1
                        continue

                    # ── Within recovery window — re-queue ────────────────────
                    prospect_tz = resolve_prospect_timezone(db, dm)
                    new_slot = calculate_next_send_slot(campaign.user_id, prospect_tz, db)

                    draft.scheduled_at = new_slot
                    draft.dispatch_state = "QUEUED"
                    # Reset started_at so stale-detection in execute_draft_send
                    # doesn't block the re-fired task
                    draft.dispatch_started_at = None
                    db.commit()

                    send_draft_worker.apply_async(
                        args=[draft.id],
                        eta=new_slot.replace(tzinfo=pytz.UTC),
                        queue="outbound_dispatch",
                    )

                    display = format_scheduled_display(new_slot, prospect_tz)
                    logger.info(
                        f"[DISPATCH-SWEEP] Recovered draft {draft.id} for {dm.name} "
                        f"→ rescheduled to {display} ({prospect_tz})."
                    )
                    recovered_count += 1

                except Exception as exc:
                    db.rollback()
                    logger.error(
                        f"[DISPATCH-SWEEP] Failed to recover draft {draft.id}: {exc}",
                        exc_info=True,
                    )
                    error_count += 1

            logger.info(
                f"[DISPATCH-SWEEP] Recovery complete — "
                f"recovered: {recovered_count}, "
                f"expired (too stale): {expired_count}, "
                f"errors: {error_count}."
            )

        except Exception as exc:
            logger.error(
                f"[DISPATCH-SWEEP] Critical error during dispatch recovery sweep: {exc}",
                exc_info=True,
            )
