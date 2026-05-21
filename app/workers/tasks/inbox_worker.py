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

def _record_mailbox_health(db, user_id: str, status: str, error: str | None = None) -> None:
    now = utcnow_naive()
    accounts = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == user_id).all()
    for account in accounts:
        account.mailbox_health_status = status
        account.mailbox_last_checked_at = now
        account.mailbox_last_error = None if status == "HEALTHY" else (error[:1000] if error else None)
def _parse_sender_email(value: str | None) -> str | None:
    if not value:
        return None
    match = EMAIL_PATTERN.search(value)
    return match.group(0).lower() if match else None


def _active_reply_filter():
    return or_(
        models.DecisionMaker.state.is_(None),
        models.DecisionMaker.state != models.ProspectState.TERMINATED,
    )


def _terminated_reply_filter():
    return or_(
        models.DecisionMaker.state == models.ProspectState.TERMINATED,
        models.DecisionMaker.status == "TERMINATED",
    )


def _find_reply_candidate(db, user_id: str, reply: dict, *, include_terminated: bool):
    state_filter = _terminated_reply_filter() if include_terminated else _active_reply_filter()

    if reply.get("thread_id"):
        thread_query = db.query(models.DecisionMaker).join(models.Campaign).filter(
            models.DecisionMaker.thread_id == reply["thread_id"],
            models.Campaign.user_id == user_id,
            state_filter,
        )
        dm = _lock_query(thread_query).first()
        if dm:
            return dm

    clean_email = _parse_sender_email(reply.get("from"))
    if not clean_email:
        return None

    cutoff = utcnow_naive() - datetime.timedelta(days=settings.REPLY_FALLBACK_WINDOW_DAYS)
    candidates = _lock_query(
        db.query(models.DecisionMaker).join(models.Campaign).filter(
            models.DecisionMaker.email == clean_email,
            models.Campaign.user_id == user_id,
            state_filter,
            or_(
                models.DecisionMaker.last_sent_at >= cutoff,
                models.DecisionMaker.last_reply_at >= cutoff,
            ),
        )
    ).all()

    if len(candidates) == 1:
        dm = candidates[0]
        if reply.get("thread_id") and not dm.thread_id:
            dm.thread_id = reply.get("thread_id")
        return dm

    if len(candidates) > 1:
        logger.warning(
            "[SENTINEL] Ambiguous email fallback for %s: %s candidates. Leaving message unread.",
            clean_email,
            len(candidates),
        )
    return None


def _match_reply_to_prospect(db, user_id: str, reply: dict):
    return _find_reply_candidate(db, user_id, reply, include_terminated=False)

def _match_reply_to_terminated_prospect(db, user_id: str, reply: dict):
    return _find_reply_candidate(db, user_id, reply, include_terminated=True)

@celery_app.task(bind=True, max_retries=3, default_retry_delay=300)
def poll_inbox_task(self, user_id: str):
    """Polls the user's mailbox for new prospect replies and classifies intent."""
    lock_key = f"inbox_sweep:{user_id}"
    if not acquire_lock(lock_key, ttl=300):
        logger.debug(f"[SENTINEL] Lock active for user {user_id}. Skipping.")
        return

    with db_session() as db:
        try:
            creds = TokenService.get_google_credentials(db, user_id)
            if not creds:
                logger.warning(f"[SENTINEL] No credentials for user {user_id}.")
                return

            provider = GmailProvider(creds)
            scan_result = provider.scan_latest_replies(unread_only=True)
            _record_mailbox_health(
                db,
                user_id,
                scan_result.get("status", "TRANSIENT_ERROR"),
                scan_result.get("error"),
            )
            db.commit()

            if scan_result.get("status") != "HEALTHY":
                logger.warning(
                    "[SENTINEL] Gmail polling unhealthy for user %s: %s",
                    user_id,
                    scan_result.get("error") or scan_result.get("status"),
                )
                if scan_result.get("status") == "AUTH_ERROR":
                    logger.warning(f"[SENTINEL] Auth failure detected. Deleting Google OAuth credentials for user {user_id}.")
                    try:
                        db.query(models.OAuthAccount).filter(
                            models.OAuthAccount.user_id == user_id,
                            models.OAuthAccount.provider == "google"
                        ).delete()
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        logger.error(f"[SENTINEL] Failed to clear Google OAuth account for user {user_id}: {exc}")
                return

            replies = scan_result.get("replies", [])
            for reply in replies:
                should_mark_read = False
                dm = None
                try:
                    with db.begin_nested():
                        dm = _match_reply_to_prospect(db, user_id, reply)
                        if not dm:
                            terminated_dm = _match_reply_to_terminated_prospect(db, user_id, reply)
                            if terminated_dm:
                                logger.info(
                                    "[SENTINEL] Ignoring late reply for terminated prospect %s (%s).",
                                    terminated_dm.name,
                                    terminated_dm.id,
                                )
                                should_mark_read = True

                        if not dm:
                            pass
                        else:
                            from app.core.logging_config import campaign_id_var

                            token = campaign_id_var.set(dm.campaign_id)
                            try:
                                if dm.state == models.ProspectState.TERMINATED or dm.status == "TERMINATED":
                                    logger.info(f"[SENTINEL] Defensive skip: {dm.name} is terminated.")
                                    should_mark_read = True
                                else:
                                    existing_log = db.query(models.CommunicationLog).filter(
                                        models.CommunicationLog.message_id == reply.get("message_id"),
                                        models.CommunicationLog.direction == "RECEIVED",
                                    ).first()
                                    if existing_log:
                                        should_mark_read = True
                                    else:
                                        inbox_service.handle_prospect_reply(db, dm, reply)
                                        should_mark_read = True
                            finally:
                                campaign_id_var.reset(token)

                    if not dm and not should_mark_read:
                        continue

                    db.commit()
                    if should_mark_read:
                        provider.mark_as_read(reply["message_id"])
                except Exception as exc:
                    db.rollback()
                    logger.error(f"[SENTINEL] Failed to process email {reply.get('message_id')}: {exc}", exc_info=True)
                    continue

        except Exception as exc:
            logger.error(f"[SENTINEL] Critical inbox sweep failure for {user_id}: {exc}", exc_info=True)
        finally:
            release_lock(lock_key)
# --- End of Inbox Polling ---
@celery_app.task
def poll_all_users_task():
    with db_session() as db:
        users = (
            db.query(models.User.id, models.User.is_demo, models.User.demo_expires_at)
            .join(models.OAuthAccount, models.OAuthAccount.user_id == models.User.id)
            .distinct()
            .all()
        )
        for user_id, is_demo, demo_expires_at in users:
            if is_demo and demo_expires_at:
                if demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                    continue
            poll_inbox_task.delay(user_id)
