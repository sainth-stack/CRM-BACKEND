"""
Sentinel Workers: inbox polling, orchestrator nudges, and meeting reminders.
"""

import datetime
import re
from datetime import UTC

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.services.inbox_service import inbox_service
from app.core.email_service import email_service
from app.core.logging_config import logger
from app.core.security import acquire_lock, release_lock
from app.core.token_service import TokenService
from app.db import models
from app.workers.utils import db_session
from app.integrations.cal import cal_provider
from app.integrations.gmail import GmailProvider
from app.integrations.hubspot import hubspot_provider
from app.workers.config.celery_app import celery_app
from app.workers.lifecycle import (
    hold_company_siblings,
    reactivate_due_prospects,
    restore_held_company_siblings,
    terminate_company_siblings,
    terminate_prospect,
    transition_prospect,
    utcnow_naive,
)

from app.core.config import settings

EMAIL_PATTERN = re.compile(r"[\w\.-]+@[\w\.-]+")


def _lock_query(query, *, skip_locked: bool = False):
    bind = query.session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        return query.with_for_update(skip_locked=skip_locked)
    return query

def _nudge_dispatch_key(dm_id: str, reminder_number: int, is_discovery: bool) -> str:
    action_type = "DISCOVERY_REMINDER" if is_discovery else "REMINDER"
    return f"{dm_id}:{action_type}:{reminder_number}"


def _begin_outbound_dispatch(
    campaign_id: str,
    dm_id: str,
    action_type: str,
    dispatch_key: str,
    *,
    started_at: datetime.datetime,
) -> tuple[bool, str | None]:
    with db_session() as dispatch_db:
        try:
            dispatch = _lock_query(
                dispatch_db.query(models.OutboundDispatch).filter(
                    models.OutboundDispatch.dispatch_key == dispatch_key
                )
            ).first()
            if dispatch:
                if dispatch.state == "SENT" and dispatch.message_id:
                    return False, "already_sent"
                if (
                    dispatch.state == "IN_PROGRESS"
                    and dispatch.dispatch_started_at
                    and dispatch.dispatch_started_at >= started_at - datetime.timedelta(minutes=settings.NUDGE_DISPATCH_STALE_MINUTES)
                ):
                    return False, "in_progress"
            else:
                dispatch = models.OutboundDispatch(
                    campaign_id=campaign_id,
                    dm_id=dm_id,
                    action_type=action_type,
                    dispatch_key=dispatch_key,
                )
                dispatch_db.add(dispatch)

            dispatch.state = "IN_PROGRESS"
            dispatch.dispatch_started_at = started_at
            dispatch.dispatch_completed_at = None
            dispatch.dispatch_error = None
            dispatch_db.commit()
            return True, None
        except Exception:
            dispatch_db.rollback()
            raise


def _mark_outbound_dispatch_state(
    dispatch_key: str,
    state: str,
    *,
    message_id: str | None = None,
    thread_id: str | None = None,
    completed_at: datetime.datetime | None = None,
    error: str | None = None,
) -> None:
    with db_session() as dispatch_db:
        try:
            dispatch = _lock_query(
                dispatch_db.query(models.OutboundDispatch).filter(
                    models.OutboundDispatch.dispatch_key == dispatch_key
                )
            ).first()
            if not dispatch:
                return
            dispatch.state = state
            if message_id:
                dispatch.message_id = message_id
            if thread_id:
                dispatch.thread_id = thread_id
            if completed_at:
                dispatch.dispatch_completed_at = completed_at
            if error:
                dispatch.dispatch_error = error[:1000]
            dispatch_db.commit()
        except Exception as exc:
            dispatch_db.rollback()
            logger.error(f"[DISPATCH] Failed to persist outbound dispatch state for {dispatch_key}: {exc}", exc_info=True)
def _audit_single_prospect(db, candidate_id: str, now: datetime.datetime) -> None:
    lock_key = f"orchestrator:dm:{candidate_id}"
    if not acquire_lock(lock_key, ttl=600):
        return

    try:
        dm = _lock_query(
            db.query(models.DecisionMaker).filter(models.DecisionMaker.id == candidate_id),
            skip_locked=True,
        ).first()
        if not dm:
            return

        from app.workers.tasks.ghostwriter_worker import draft_followup_worker

        state = dm.state
        if state in [
            models.ProspectState.INITIAL_SENT,
            models.ProspectState.FOLLOWUP_ACTIVE,
            models.ProspectState.REMINDER_1_SENT,
            models.ProspectState.REMINDER_2_SENT,
        ]:
            if dm.reminder_count < 2:
                reminder_number = dm.reminder_count + 1
                new_state = (
                    models.ProspectState.REMINDER_1_SENT
                    if reminder_number == 1
                    else models.ProspectState.REMINDER_2_SENT
                )
                logger.info(f"[ORCHESTRATOR] Inactivity detected for {dm.name}. Deploying Reminder #{reminder_number}")
                _deploy_nudge(db, dm, new_state, reminder_number=reminder_number)
            else:
                logger.info(f"[ORCHESTRATOR] Maximum silence reached for {dm.name}. Terminating mission.")
                terminate_prospect(
                    db, dm,
                    models.ProspectTerminationReason.NO_RESPONSE,
                    retryable=True, now=now, actor="orchestrator",
                )
                hubspot_provider.update_lead_status(dm.hubspot_id, "Terminated (System Timeout)")
        elif state == models.ProspectState.WAITING_FOR_REPLY:
            reminder_number = dm.reminder_count + 1
            logger.info(f"[ORCHESTRATOR] Discovery scheduling timeout for {dm.name}. Deploying reminder #{reminder_number}")
            _deploy_nudge(
                db, dm, models.ProspectState.WAITING_FOR_REPLY,
                is_discovery=True, reminder_number=reminder_number,
                expire_after_send=reminder_number >= 2,
            )
        elif state == models.ProspectState.NEUTRAL:
            if dm.followup_count < settings.MAX_NEUTRAL_FOLLOWUPS:
                logger.info(f"[ORCHESTRATOR] Processing Neutral persistence for {dm.name} (Nudge #{dm.followup_count + 1})")
                draft_followup_worker(dm.id, db=db)
            else:
                logger.info(f"[ORCHESTRATOR] Exhausted neutral follow-ups for {dm.name}. Terminating.")
                terminate_prospect(
                    db, dm,
                    models.ProspectTerminationReason.FOLLOWUP_EXHAUSTED,
                    retryable=True, now=now, actor="orchestrator",
                )
                hubspot_provider.update_lead_status(dm.hubspot_id, "Terminated (Exhausted Threshold)")

        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"[ORCHESTRATOR] Protocol failure for DM {candidate_id}: {exc}", exc_info=True)
    finally:
        release_lock(lock_key)


@celery_app.task
def outreach_orchestrator_worker():
    """Audits the lifecycle of all active outreach prospects and triggers reminders."""
    logger.info("[ORCHESTRATOR] Auditing active outreach lifecycle...")
    with db_session() as db:
        try:
            now = utcnow_naive()
            prospect_ids = (
                db.query(models.DecisionMaker.id)
                .filter(
                    models.DecisionMaker.next_action_at <= now,
                    models.DecisionMaker.state.notin_(
                        [
                            models.ProspectState.TERMINATED,
                            models.ProspectState.MEETING_BOOKED,
                            models.ProspectState.ON_HOLD,
                            models.ProspectState.DISCOVERY_EXPIRED,
                        ]
                    ),
                )
                .all()
            )

            for (candidate_id,) in prospect_ids:
                _audit_single_prospect(db, candidate_id, now)
        except Exception as exc:
            logger.error(f"[ORCHESTRATOR] Master audit failure: {exc}", exc_info=True)


def _apply_nudge_effects(
    db,
    dm,
    *,
    next_state,
    reminder_number: int,
    subject: str,
    body: str,
    message_id: str,
    thread_id: str | None,
    sent_at: datetime.datetime,
    is_discovery: bool,
    expire_after_send: bool,
) -> list[tuple[str | None, str]]:
    existing_log = db.query(models.CommunicationLog).filter(
        models.CommunicationLog.message_id == message_id,
        models.CommunicationLog.direction == "SENT",
    ).first()
    if not existing_log:
        db.add(
            models.CommunicationLog(
                campaign_id=dm.campaign_id,
                dm_id=dm.id,
                direction="SENT",
                subject=subject,
                body=body,
                message_id=message_id,
                received_at=sent_at,
            )
        )

    dm.last_message_id = message_id
    dm.thread_id = thread_id or dm.thread_id
    dm.last_sent_at = sent_at
    dm.reminder_count = reminder_number
    dm.termination_reason = None
    dm.retry_after = None

    hubspot_updates: list[tuple[str | None, str]] = []
    transition_prospect(
        db,
        dm,
        state=next_state,
        status=next_state.value,
        reason="REMINDER_SENT" if not is_discovery else "DISCOVERY_REMINDER_SENT",
        actor="orchestrator",
        metadata={
            "is_discovery": is_discovery,
            "message_id": message_id,
            "reminder_count": reminder_number,
        },
    )

    if is_discovery:
        hubspot_updates.append((dm.hubspot_id, "Discovery Reminder Sent"))
        if expire_after_send:
            logger.info(
                f"[ORCHESTRATOR] Discovery engagement expired for {dm.name}. Restoring held siblings and switching to passive monitoring."
            )
            for sibling in restore_held_company_siblings(db, dm):
                hubspot_updates.append((sibling.hubspot_id, "Hold Released"))
            transition_prospect(
                db,
                dm,
                state=models.ProspectState.DISCOVERY_EXPIRED,
                status="DISCOVERY_EXPIRED",
                reason="DISCOVERY_EXPIRED",
                actor="orchestrator",
                metadata={"reminder_count": reminder_number, "message_id": message_id},
            )
            dm.next_action_at = None
            if dm.target_company and dm.target_company.status != "MEETING_BOOKED":
                dm.target_company.status = "ACTIVE"
            hubspot_updates.append((dm.hubspot_id, "Discovery Expired"))
        else:
            dm.next_action_at = sent_at + datetime.timedelta(days=settings.NUDGE_FOLLOWUP_DELAY_DAYS)
    else:
        dm.next_action_at = sent_at + datetime.timedelta(days=settings.NUDGE_FOLLOWUP_DELAY_DAYS)
        hubspot_updates.append((dm.hubspot_id, f"{next_state.value} Sent"))

    return hubspot_updates


def _recover_nudge_persistence(
    dm_id: str,
    dispatch_key: str,
    *,
    next_state,
    reminder_number: int,
    subject: str,
    body: str,
    message_id: str,
    thread_id: str | None,
    sent_at: datetime.datetime,
    is_discovery: bool,
    expire_after_send: bool,
    dispatch_error: str,
) -> tuple[bool, list[tuple[str | None, str]]]:
    with db_session() as recovery_db:
        try:
            dm = _lock_query(
                recovery_db.query(models.DecisionMaker)
                .options(joinedload(models.DecisionMaker.campaign).joinedload(models.Campaign.user_intel))
                .options(joinedload(models.DecisionMaker.target_company))
                .filter(models.DecisionMaker.id == dm_id)
            ).first()
            if not dm:
                _mark_outbound_dispatch_state(dispatch_key, "FAILED", error=dispatch_error)
                return False, []

            hubspot_updates = _apply_nudge_effects(
                recovery_db, dm,
                next_state=next_state, reminder_number=reminder_number,
                subject=subject, body=body, message_id=message_id,
                thread_id=thread_id, sent_at=sent_at,
                is_discovery=is_discovery, expire_after_send=expire_after_send,
            )
            recovery_db.commit()
            _mark_outbound_dispatch_state(
                dispatch_key, "REQUIRES_REVIEW",
                message_id=message_id, thread_id=thread_id,
                completed_at=sent_at, error=dispatch_error,
            )
            logger.error(f"[DISPATCH] Recovered outbound nudge {dispatch_key} after post-send persistence failure.")
            return True, hubspot_updates
        except Exception as exc:
            recovery_db.rollback()
            _mark_outbound_dispatch_state(
                dispatch_key, "FAILED",
                message_id=message_id, thread_id=thread_id,
                error=f"{dispatch_error} | Recovery failure: {exc}",
            )
            logger.error(f"[DISPATCH] Recovery failed for outbound nudge {dispatch_key}: {exc}", exc_info=True)
            return False, []


def _deploy_nudge(
    db,
    dm,
    next_state,
    *,
    reminder_number: int,
    is_discovery: bool = False,
    expire_after_send: bool = False,
):
    dispatch_key = _nudge_dispatch_key(dm.id, reminder_number, is_discovery)
    action_type = "DISCOVERY_REMINDER" if is_discovery else "REMINDER"
    started_at = utcnow_naive()

    allowed, blocked_reason = _begin_outbound_dispatch(
        dm.campaign_id,
        dm.id,
        action_type,
        dispatch_key,
        started_at=started_at,
    )
    if not allowed:
        logger.info(
            "[ORCHESTRATOR] Skipping nudge %s for %s because dispatch is %s.",
            dispatch_key,
            dm.name,
            blocked_reason,
        )
        return False

    msg_data = None
    sent_at = None
    subject = None
    body = None
    hubspot_updates: list[tuple[str | None, str]] = []
    try:
        from app.agents.email_drafter import draft_nudge_email

        last_sent = (
            db.query(models.CommunicationLog)
            .filter(
                models.CommunicationLog.dm_id == dm.id,
                models.CommunicationLog.direction == "SENT",
            )
            .order_by(models.CommunicationLog.received_at.desc())
            .first()
        )

        if is_discovery:
            body = (
                f"Hi {dm.name},\n\n"
                f"Just checking whether you had a chance to share a few time options for the discovery call "
                f"with {dm.campaign.user_intel.company_name}.\n\n"
                "Happy to work around your schedule.\n"
            )
        else:
            body = draft_nudge_email(dm.name, dm.campaign.user_intel.company_name)
        subject = f"Re: {last_sent.subject}" if last_sent else "Checking in"

        creds = TokenService.get_google_credentials(db, dm.campaign.user_id)
        msg_data = email_service.send_email(
            to_email=dm.email,
            subject=subject,
            body=body,
            creds=creds,
            thread_id=dm.thread_id,
        )

        sent_at = utcnow_naive()
        hubspot_updates = _apply_nudge_effects(
            db,
            dm,
            next_state=next_state,
            reminder_number=reminder_number,
            subject=subject,
            body=body,
            message_id=msg_data["id"],
            thread_id=msg_data.get("thread_id"),
            sent_at=sent_at,
            is_discovery=is_discovery,
            expire_after_send=expire_after_send,
        )
        db.commit()
        _mark_outbound_dispatch_state(
            dispatch_key,
            "SENT",
            message_id=msg_data["id"],
            thread_id=msg_data.get("thread_id"),
            completed_at=sent_at,
        )
        for hubspot_id, status in hubspot_updates:
            hubspot_provider.update_lead_status(hubspot_id, status)
        return True
    except Exception as exc:
        db.rollback()
        if msg_data and sent_at and subject is not None and body is not None:
            recovered, recovered_updates = _recover_nudge_persistence(
                dm.id,
                dispatch_key,
                next_state=next_state,
                reminder_number=reminder_number,
                subject=subject,
                body=body,
                message_id=msg_data["id"],
                thread_id=msg_data.get("thread_id"),
                sent_at=sent_at,
                is_discovery=is_discovery,
                expire_after_send=expire_after_send,
                dispatch_error=f"Recovered after post-send persistence error: {exc}",
            )
            if recovered:
                for hubspot_id, status in recovered_updates:
                    try:
                        hubspot_provider.update_lead_status(hubspot_id, status)
                    except Exception:
                        logger.warning(f"[DISPATCH] CRM sync still pending after recovery for nudge {dispatch_key}.")
                return True

        _mark_outbound_dispatch_state(dispatch_key, "FAILED", error=str(exc))
        logger.error(f"[ORCHESTRATOR] Nudge deployment failed for {dm.name}: {exc}", exc_info=True)
        raise

@celery_app.task
def reactivate_terminated_prospects_task():
    with db_session() as db:
        try:
            from app.workers.tasks.ghostwriter_worker import draft_followup_worker
            prospects = reactivate_due_prospects(db)
            db.commit()
            for dm in prospects:
                draft_followup_worker(dm.id, db=db)
                hubspot_provider.update_lead_status(dm.hubspot_id, "Reactivated")
        except Exception as exc:
            db.rollback()
            logger.error(f"[RETRY] Reactivation cycle failed: {exc}", exc_info=True)

@celery_app.task
def check_all_inactivity_task():
    outreach_orchestrator_worker()
