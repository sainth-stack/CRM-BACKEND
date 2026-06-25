"""Outbound email dispatch.

Reliability model (production-grade):

  * The SCHEDULE lives in the database — `EmailDraft.scheduled_at` +
    `dispatch_state='QUEUED'`. It is the single source of truth.
  * A periodic poller (`dispatch_due_drafts_task`, Celery Beat every
    DISPATCH_POLL_SECONDS) reconciles that schedule against reality. It finds
    drafts whose time has come and dispatches them. Because it is DB-driven it
    survives anything that loses a Celery message — server restart, worker crash,
    Redis/broker flush. (The old design relied on a single long `eta` task per
    draft; if the server was down when that `eta` fired, the email was silently
    lost. That failure mode is gone.)
  * Sends are rate-limited per user via a Redis gate so a recovered backlog
    drains gradually (no blast) instead of all firing at once.
  * The actual send (`send_draft_worker` -> `execute_draft_send`) is fully
    idempotent (per-draft lock + SENT/message_id checks + post-send recovery),
    so even if two triggers race, an email is sent at most once.

NOTE: the previous cascade bug — where `send_draft_worker` re-ran
`calculate_next_send_slot` on every fire and thus re-staggered each email *behind
its own batch-mates*, smearing a one-day batch across many days — is removed.
The fire-time path never re-staggers; it only sends (if in window) or bumps to
the next window (if the window has passed).
"""
import datetime
from collections import defaultdict
from datetime import timezone

from app.core.config import settings
from app.core.logging_config import logger
from app.core.security import acquire_lock, release_lock
from app.services.draft_dispatch import execute_draft_send
from app.services.observability_service import ObservabilityService
from app.workers.config.celery_app import celery_app
from app.workers.utils import db_session

UTC = timezone.utc

_POLLER_LOCK = "dispatch_due_drafts:poller"
_MAX_PER_RUN    = settings.MAX_DISPATCH_PER_RUN   # cap drafts processed per poll (next run continues)
_MAX_STALE_DAYS = settings.MAX_STALE_DRAFT_DAYS   # older than this -> FAILED for manual review


def _stagger_seconds() -> int:
    from app.core.scheduler import STAGGER_MINUTES
    return max(1, int(STAGGER_MINUTES) * 60)


def _user_gate_open(user_id: str, ttl_seconds: int) -> bool:
    """Per-user send gate. Returns True (and claims the gate for ttl_seconds) when
    a send is allowed; False when another send happened within the gate window.
    Fail-OPEN on any Redis problem — we must never block legitimate sends because
    the rate-limiter is unavailable; in-run ordering still provides some spacing.
    """
    from app.core.security import _get_redis
    r = _get_redis()
    if not r:
        return True
    try:
        return bool(r.set(f"dispatch_gate:{user_id}", "1", nx=True, ex=ttl_seconds))
    except Exception as exc:
        logger.warning(f"[DISPATCH-POLL] gate check failed ({exc}); failing open.")
        return True


# --------------------------------------------------------------------------- #
# The durable reconciliation poller                                           #
# --------------------------------------------------------------------------- #
@celery_app.task(name="app.workers.tasks.outbound_worker.dispatch_due_drafts_task")
def dispatch_due_drafts_task():
    """Send every draft whose scheduled time has arrived. The single, durable
    trigger for outbound delivery."""
    from app.db import models
    from app.core.scheduler import resolve_prospect_timezone, is_in_delivery_window, next_window_slot

    # Only one poller at a time (Beat could overlap if a run is slow).
    if not acquire_lock(_POLLER_LOCK, ttl=settings.DISPATCH_POLL_SECONDS + 30):
        logger.debug("[DISPATCH-POLL] Previous poll still running; skipping.")
        return

    try:
        now_utc = datetime.datetime.now(UTC)
        now_naive = now_utc.replace(tzinfo=None)
        stagger = _stagger_seconds()

        # ── A) Retire drafts that have been stranded too long ───────────────
        with db_session() as db:
            stale_cutoff = now_naive - datetime.timedelta(days=_MAX_STALE_DAYS)
            stale = (
                db.query(models.EmailDraft)
                .filter(
                    models.EmailDraft.dispatch_state == "QUEUED",
                    models.EmailDraft.status != "SENT",
                    models.EmailDraft.scheduled_at.isnot(None),
                    models.EmailDraft.scheduled_at < stale_cutoff,
                )
                .all()
            )
            for d in stale:
                d.dispatch_state = "FAILED"
                d.dispatch_error = (
                    f"Stranded more than {_MAX_STALE_DAYS} days "
                    f"(scheduled {d.scheduled_at}). Manual review required."
                )
            if stale:
                db.commit()
                logger.warning(f"[DISPATCH-POLL] Marked {len(stale)} over-stale draft(s) FAILED.")

        # ── B) Collect drafts whose time has come ───────────────────────────
        with db_session() as db:
            due = (
                db.query(models.EmailDraft.id, models.Campaign.user_id)
                .join(models.Campaign)
                .filter(
                    models.EmailDraft.dispatch_state == "QUEUED",
                    models.EmailDraft.status != "SENT",
                    models.EmailDraft.scheduled_at.isnot(None),
                    models.EmailDraft.scheduled_at <= now_naive,
                )
                .order_by(models.EmailDraft.scheduled_at.asc())
                .limit(_MAX_PER_RUN)
                .all()
            )
            due_by_user: dict[str, list[str]] = defaultdict(list)
            for draft_id, user_id in due:
                due_by_user[user_id].append(draft_id)

        if not due_by_user:
            return

        total = sum(len(v) for v in due_by_user.values())
        logger.info(f"[DISPATCH-POLL] {total} due draft(s) across {len(due_by_user)} user(s).")

        sent, bumped, throttled, failed = 0, 0, 0, 0

        # ── C) Dispatch in scheduled order, rate-gated per user ─────────────
        for user_id, draft_ids in due_by_user.items():
            for draft_id in draft_ids:
                try:
                    with db_session() as db:
                        draft = (
                            db.query(models.EmailDraft)
                            .filter(models.EmailDraft.id == draft_id)
                            .first()
                        )
                        # Re-validate against the freshest state (idempotency).
                        if not draft or draft.status == "SENT" or draft.dispatch_state != "QUEUED":
                            continue

                        dm = draft.dm
                        if not dm or not dm.email:
                            draft.dispatch_state = "FAILED"
                            draft.dispatch_error = "No decision-maker email at dispatch time."
                            db.commit()
                            failed += 1
                            continue

                        prospect_tz = resolve_prospect_timezone(db, dm)

                        # Window passed (e.g. server was down) -> bump, never send off-hours.
                        if not is_in_delivery_window(now_utc, prospect_tz):
                            draft.scheduled_at = next_window_slot(now_utc, prospect_tz)
                            db.commit()
                            bumped += 1
                            continue

                        # In window -> per-user rate gate.
                        if _user_gate_open(user_id, stagger):
                            db.commit()  # persist any tz resolution before handing off
                            send_draft_worker.delay(draft_id)
                            sent += 1
                        else:
                            # Too soon after the last send for this user -> push it
                            # forward by one stagger step (stays in window/next window).
                            start = now_utc + datetime.timedelta(seconds=stagger)
                            draft.scheduled_at = next_window_slot(start, prospect_tz)
                            db.commit()
                            throttled += 1
                except Exception as exc:
                    logger.error(f"[DISPATCH-POLL] Error on draft {draft_id}: {exc}", exc_info=True)

        logger.info(
            f"[DISPATCH-POLL] dispatched={sent} bumped={bumped} throttled={throttled} failed={failed}"
        )
    finally:
        release_lock(_POLLER_LOCK)


# --------------------------------------------------------------------------- #
# The send executor (enqueued by the poller once a draft is due + in-window)   #
# --------------------------------------------------------------------------- #
@celery_app.task(bind=True, name="app.workers.tasks.outbound_worker.send_draft_worker")
@ObservabilityService.track_celery_task("send_draft_worker")
def send_draft_worker(self, draft_id: str):
    """Send one draft. Pure executor — NO re-staggering (that caused the cascade).

    Does a final delivery-window safety check (covers the small poller→worker
    latency and any manual re-trigger), then performs the idempotent send. If the
    window has closed in the meantime, it bumps the schedule and lets the poller
    pick it up again rather than sending off-hours.
    """
    from app.db.database import SessionLocal
    from app.db import models
    from app.core.scheduler import (
        resolve_prospect_timezone,
        is_in_delivery_window,
        next_window_slot,
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
            logger.warning(f"[OUTBOUND] Draft {draft_id} not found.")
            return {"status": "not_found"}
        if draft.status == "SENT":
            return {"status": "already_sent"}

        dm = draft.dm
        if dm:
            now_utc = datetime.datetime.now(UTC)
            prospect_tz = resolve_prospect_timezone(db, dm)
            if not is_in_delivery_window(now_utc, prospect_tz):
                draft.scheduled_at = next_window_slot(now_utc, prospect_tz)
                draft.dispatch_state = "QUEUED"
                draft.dispatch_started_at = None
                db.commit()
                logger.info(
                    f"[OUTBOUND] Draft {draft_id} fired outside window — "
                    f"bumped to {draft.scheduled_at} (poller will re-dispatch)."
                )
                ObservabilityService.increment_metric("dispatch_rescheduled")
                return {"status": "rescheduled", "new_slot_utc": str(draft.scheduled_at)}
            db.commit()  # persist any timezone resolution
    finally:
        db.close()

    result = execute_draft_send(draft_id)
    if result["status"] == "failed":
        logger.error(f"[OUTBOUND] Draft {draft_id} failed: {result['message']}")
    return result
