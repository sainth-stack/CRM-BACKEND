import datetime
from datetime import UTC

from sqlalchemy.orm import Session

from app.core.email_service import email_service
from app.core.logging_config import logger
from app.core.security import acquire_lock, release_lock
from app.core.token_service import TokenService
from app.db import models
from app.db.database import SessionLocal


from app.core.config import settings

DRAFT_DISPATCH_STALE_MINUTES = settings.NUDGE_DISPATCH_STALE_MINUTES


def lock_query(query):
    bind = query.session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        return query.with_for_update()
    return query


def _apply_sent_draft_effects(
    db: Session,
    db_draft: models.EmailDraft,
    *,
    message_id: str,
    thread_id: str | None,
    rfc_message_id: str | None = None,
    sent_at: datetime.datetime,
    dispatch_state: str = "SENT",
    dispatch_error: str | None = None,
) -> None:
    from app.workers.lifecycle import arm_company_hold_window, transition_prospect

    existing_log = db.query(models.CommunicationLog).filter(
        models.CommunicationLog.message_id == message_id,
        models.CommunicationLog.direction == "SENT",
    ).first()
    if not existing_log:
        db.add(
            models.CommunicationLog(
                campaign_id=db_draft.campaign_id,
                dm_id=db_draft.decision_maker_id,
                direction="SENT",
                subject=db_draft.subject,
                body=db_draft.body,
                message_id=message_id,
                received_at=sent_at,
            )
        )

    if db_draft.dm:
        dm = db_draft.dm
        dm.last_message_id = message_id
        dm.thread_id = thread_id or dm.thread_id
        dm.last_rfc_message_id = rfc_message_id or dm.last_rfc_message_id
        dm.last_sent_at = sent_at
        dm.termination_reason = None
        dm.retry_after = None

        draft_type = getattr(db_draft, "draft_type", "INITIAL")
        if draft_type == "DISCOVERY":
            dm.reminder_count = 0
            transition_prospect(
                db,
                dm,
                state=models.ProspectState.WAITING_FOR_REPLY,
                status="WAITING_FOR_REPLY",
                reason="DISCOVERY_SENT",
                actor="user",
                metadata={"draft_type": draft_type, "draft_id": db_draft.id, "message_id": message_id},
            )
            dm.next_action_at = dm.last_sent_at + datetime.timedelta(minutes=settings.NUDGE_FOLLOWUP_DELAY_MINUTES)
            arm_company_hold_window(db, dm, sent_at=sent_at)
        elif draft_type == "INITIAL":
            dm.reminder_count = 0
            transition_prospect(
                db,
                dm,
                state=models.ProspectState.INITIAL_SENT,
                status="INITIAL_SENT",
                reason="INITIAL_SENT",
                actor="user",
                metadata={"draft_type": draft_type, "draft_id": db_draft.id, "message_id": message_id},
            )
            dm.next_action_at = dm.last_sent_at + datetime.timedelta(minutes=settings.NUDGE_FOLLOWUP_DELAY_MINUTES)
        elif draft_type == "REMINDER":
            from app.services.reminder_sequence import mark_reminder_sent_effects
            mark_reminder_sent_effects(
                db,
                db_draft,
                dm,
                message_id=message_id,
                sent_at=sent_at,
            )
        else:
            dm.reminder_count = 0
            idx = db_draft.followup_index
            dm.followup_count = idx
            transition_prospect(
                db,
                dm,
                state=models.ProspectState.FOLLOWUP_ACTIVE,
                status=f"FOLLOWUP_{idx}_SENT",
                reason="FOLLOWUP_SENT",
                actor="user",
                metadata={"draft_type": draft_type, "draft_id": db_draft.id, "message_id": message_id, "followup_index": idx},
            )
            dm.next_action_at = dm.last_sent_at + datetime.timedelta(minutes=settings.NUDGE_FOLLOWUP_DELAY_MINUTES)

    db_draft.status = "SENT"
    db_draft.message_id = message_id
    db_draft.sent_at = sent_at
    db_draft.dispatch_state = dispatch_state
    db_draft.dispatch_completed_at = sent_at
    db_draft.dispatch_error = dispatch_error


def _mark_draft_dispatch_failure(draft_id: str, error_message: str) -> None:
    failure_db = SessionLocal()
    try:
        draft = lock_query(
            failure_db.query(models.EmailDraft).filter(models.EmailDraft.id == draft_id)
        ).first()
        if draft and draft.status != "SENT":
            draft.dispatch_state = "FAILED"
            draft.dispatch_error = error_message[:1000]
            failure_db.commit()
    except Exception as exc:
        failure_db.rollback()
        logger.error(f"[DISPATCH] Failed to persist draft failure state for {draft_id}: {exc}", exc_info=True)
    finally:
        failure_db.close()


def _recover_sent_draft_persistence(
    draft_id: str,
    *,
    message_id: str,
    thread_id: str | None,
    rfc_message_id: str | None = None,
    sent_at: datetime.datetime,
    dispatch_error: str,
) -> bool:
    recovery_db = SessionLocal()
    try:
        draft = lock_query(
            recovery_db.query(models.EmailDraft).filter(models.EmailDraft.id == draft_id)
        ).first()
        if not draft:
            return False

        if draft.status == "SENT" and draft.message_id == message_id:
            return True

        _apply_sent_draft_effects(
            recovery_db,
            draft,
            message_id=message_id,
            thread_id=thread_id,
            rfc_message_id=rfc_message_id,
            sent_at=sent_at,
            dispatch_state="REQUIRES_REVIEW",
            dispatch_error=dispatch_error,
        )
        recovery_db.commit()
        logger.error(
            f"[DISPATCH] Recovered sent draft {draft_id} after post-send persistence failure."
        )
        return True
    except Exception as exc:
        recovery_db.rollback()
        logger.error(f"[DISPATCH] Recovery failed for sent draft {draft_id}: {exc}", exc_info=True)
        return False
    finally:
        recovery_db.close()


def queue_draft_dispatch(
    db: Session,
    db_draft: models.EmailDraft,
    *,
    queued_at: datetime.datetime | None = None,
) -> str:
    now = queued_at or datetime.datetime.now(UTC).replace(tzinfo=None)

    if db_draft.status == "SENT" and db_draft.message_id:
        return "already_sent"

    if db_draft.dispatch_state == "IN_PROGRESS":
        if (
            db_draft.dispatch_started_at
            and db_draft.dispatch_started_at >= now - datetime.timedelta(minutes=DRAFT_DISPATCH_STALE_MINUTES)
        ):
            return "in_progress"
        return "requires_review"

    if db_draft.dispatch_state == "QUEUED":
        if (
            db_draft.dispatch_started_at
            and db_draft.dispatch_started_at >= now - datetime.timedelta(minutes=DRAFT_DISPATCH_STALE_MINUTES)
        ):
            return "queued"

    db_draft.dispatch_state = "QUEUED"
    db_draft.dispatch_started_at = now
    db_draft.dispatch_completed_at = None
    db_draft.dispatch_error = None
    return "queued"


def execute_draft_send(draft_id: str) -> dict[str, str]:
    db = SessionLocal()
    lock_key = f"send_draft:{draft_id}"
    msg_id = None
    thread_id = None
    rfc_message_id = None
    sent_at = None

    try:
        db_draft = lock_query(
            db.query(models.EmailDraft).join(models.Campaign).filter(
                models.EmailDraft.id == draft_id
            )
        ).first()
        if not db_draft:
            return {"status": "not_found", "message": "Draft not found."}

        if db_draft.status == "SENT" and db_draft.message_id:
            return {"status": "already_sent", "message": "Engagement protocol already deployed."}

        now = datetime.datetime.now(UTC).replace(tzinfo=None)
        if db_draft.dispatch_state == "IN_PROGRESS":
            if (
                db_draft.dispatch_started_at
                and db_draft.dispatch_started_at >= now - datetime.timedelta(minutes=DRAFT_DISPATCH_STALE_MINUTES)
            ):
                return {"status": "in_progress", "message": "Draft deployment already in progress."}
            return {
                "status": "requires_review",
                "message": "Previous deployment attempt is unresolved and needs review.",
            }

        if db_draft.dispatch_state not in {"QUEUED", "FAILED", "REQUIRES_REVIEW", "IDLE"}:
            return {"status": "ignored", "message": f"Draft dispatch in state {db_draft.dispatch_state} cannot be executed."}

        prospect_email = db_draft.dm.email if db_draft.dm else None
        if not prospect_email:
            raise ValueError("Deployment coordinate (Email) missing. Please refine and synchronize stakeholder data.")

        if not acquire_lock(lock_key, ttl=120):
            return {"status": "in_progress", "message": "Draft deployment lock already held."}

        db_draft.dispatch_state = "IN_PROGRESS"
        db_draft.dispatch_started_at = now
        db_draft.dispatch_completed_at = None
        db_draft.dispatch_error = None
        db.commit()

        creds = TokenService.get_google_credentials(db, db_draft.campaign.user_id)
        thread_id = db_draft.dm.thread_id if db_draft.dm else None
        in_reply_to = db_draft.dm.last_rfc_message_id if db_draft.dm else None
        msg_data = email_service.send_email(
            to_email=prospect_email,
            subject=db_draft.subject,
            body=db_draft.body,
            creds=creds,
            thread_id=thread_id,
            in_reply_to_message_id=in_reply_to,
        )
        msg_id = msg_data["id"]
        thread_id = msg_data["thread_id"]
        rfc_message_id = msg_data.get("rfc_message_id")

        from app.services.observability_service import ObservabilityService

        sent_at = datetime.datetime.now(UTC).replace(tzinfo=None)
        _apply_sent_draft_effects(
            db,
            db_draft,
            message_id=msg_id,
            thread_id=thread_id,
            rfc_message_id=rfc_message_id,
            sent_at=sent_at,
        )
        db.commit()

        ObservabilityService.increment_metric("dispatch_success")
        return {"status": "sent", "message": "Engagement protocol mobilized."}
    except Exception as exc:
        db.rollback()
        from app.services.observability_service import ObservabilityService
        if msg_id and sent_at:
            recovered = _recover_sent_draft_persistence(
                draft_id,
                message_id=msg_id,
                thread_id=thread_id,
                rfc_message_id=rfc_message_id,
                sent_at=sent_at,
                dispatch_error=f"Recovered after post-send persistence error: {str(exc)}",
            )
            if recovered:
                ObservabilityService.increment_metric("dispatch_recovered")
                return {
                    "status": "recovered",
                    "message": "Engagement protocol mobilized. Internal reconciliation completed after a transient persistence failure.",
                }

        _mark_draft_dispatch_failure(draft_id, str(exc))
        ObservabilityService.increment_metric("dispatch_failed")
        logger.error(f"Failed to dispatch draft {draft_id}: {exc}", exc_info=True)
        return {"status": "failed", "message": f"Failed to dispatch: {str(exc)}"}
    finally:
        release_lock(lock_key)
        db.close()
