import datetime
from datetime import timezone

import pytz

from app.core.logging_config import logger
from app.services.draft_dispatch import execute_draft_send
from app.workers.config.celery_app import celery_app
from app.services.observability_service import ObservabilityService

UTC = timezone.utc

# If calculate_next_send_slot returns a slot more than this many seconds away,
# reschedule via apply_async(eta=slot) instead of sending immediately.
#
# Why 60 s?
#   • No queue conflicts + inside window  → slot = now + 10 s  (<60 → send now)
#   • Any queue conflict                  → slot = now + STAGGER_MINUTES (180 s+)
#   • Outside window / weekend            → slot = hours or days away
# Any real reason to wait will produce a slot well above 60 s.
_RESCHEDULE_THRESHOLD_SECONDS = 60


@celery_app.task(bind=True, name="app.workers.tasks.outbound_worker.send_draft_worker")
@ObservabilityService.track_celery_task("send_draft_worker")
def send_draft_worker(self, draft_id: str):
    """
    Background draft dispatcher with smart slot guard.

    Every time this task fires — whether on-time or after a server recovery —
    it calls calculate_next_send_slot() to determine the correct send time.
    That function checks both delivery windows and existing queued emails,
    so it naturally handles:

      • Stagger conflicts  – other emails already queued in this window
        → slot pushed forward by STAGGER_MINUTES per conflict
      • Outside window     – task fired after window closed (server was down)
        → slot is the next open window today, or tomorrow if none remain
      • Weekend            – slot is next Monday 9:30 AM

    Decision:
      slot within _RESCHEDULE_THRESHOLD_SECONDS → send now
      slot further away                          → reschedule via eta
    """
    from app.db.database import SessionLocal
    from app.db import models
    from app.core.scheduler import (
        resolve_prospect_timezone,
        calculate_next_send_slot,
        format_scheduled_display,
    )

    db = SessionLocal()
    try:
        draft = (
            db.query(models.EmailDraft)
            .join(models.Campaign)
            .filter(models.EmailDraft.id == draft_id)
            .first()
        )
        if not draft:
            logger.warning(f"[OUTBOUND] Draft {draft_id} not found — skipping slot check.")
        elif draft.status == "SENT":
            return {"status": "already_sent", "message": "Draft already dispatched."}
        else:
            dm = draft.dm
            campaign = draft.campaign

            if dm and campaign:
                now_utc = datetime.datetime.now(UTC)
                prospect_tz = resolve_prospect_timezone(db, dm)

                # Next proper slot — window-aligned and stagger-safe
                new_slot = calculate_next_send_slot(campaign.user_id, prospect_tz, db)
                now_naive = now_utc.replace(tzinfo=None)
                seconds_away = (new_slot - now_naive).total_seconds()

                if seconds_away > _RESCHEDULE_THRESHOLD_SECONDS:
                    # Slot is in the future: reschedule and exit without sending
                    draft.scheduled_at = new_slot
                    draft.dispatch_state = "QUEUED"
                    draft.dispatch_started_at = None
                    db.commit()

                    send_draft_worker.apply_async(
                        args=[draft_id],
                        eta=new_slot.replace(tzinfo=pytz.UTC),
                        queue="outbound_dispatch",
                    )

                    display = format_scheduled_display(new_slot, prospect_tz)
                    logger.warning(
                        f"[OUTBOUND] Draft {draft_id} ({dm.name}) rescheduled — "
                        f"next available slot: {display} ({prospect_tz}) "
                        f"[{int(seconds_away)}s away]."
                    )
                    ObservabilityService.increment_metric("dispatch_rescheduled")
                    return {
                        "status": "rescheduled",
                        "new_slot_utc": str(new_slot),
                        "display": display,
                        "seconds_away": int(seconds_away),
                    }
                # Slot is within threshold — we're in a window with no conflicts
    finally:
        db.close()

    # Proceed to send now
    result = execute_draft_send(draft_id)
    if result["status"] == "failed":
        logger.error(
            f"[OUTBOUND] Draft {draft_id} failed in background dispatch: {result['message']}"
        )
    return result
