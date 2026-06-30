"""
Background worker: upcoming-meeting reminder emails (24h / 1h).
"""

import datetime
from datetime import UTC

from sqlalchemy.orm import joinedload

from app.core.email_service import email_service
from app.core.logging_config import logger
from app.core.token_service import TokenService
from app.db import models
from app.workers.config.celery_app import celery_app
from app.workers.utils import db_session

from app.core.config import settings


@celery_app.task
def check_upcoming_meetings_task():
    now = datetime.datetime.now(UTC)

    # 1. Short read session: load only booked prospect IDs, then RELEASE the connection.
    #    The actual reminder send (network email) runs per-prospect with its own
    #    session, so no single Neon connection is pinned across the whole loop.
    with db_session() as db:
        booked_dm_ids = [
            row[0]
            for row in db.query(models.DecisionMaker.id)
            .filter(models.DecisionMaker.status == "MEETING_BOOKED")
            .all()
        ]

    # 2. Per-prospect short session: held only for one prospect's work, released
    #    before the next. One prospect's failure is isolated from the rest.
    for dm_id in booked_dm_ids:
        try:
            with db_session() as db:
                dm = (
                    db.query(models.DecisionMaker)
                    .options(joinedload(models.DecisionMaker.campaign).joinedload(models.Campaign.user_intel))
                    .filter(models.DecisionMaker.id == dm_id)
                    .first()
                )
                if not dm or not dm.scheduled_time_utc:
                    continue
                meeting_time = dm.scheduled_time_utc.replace(tzinfo=UTC)
                time_until = meeting_time - now
                if datetime.timedelta(hours=settings.REMINDER_24H_MIN_HOURS) < time_until < datetime.timedelta(hours=settings.REMINDER_24H_MAX_HOURS):
                    if not dm.reminder_24h_sent:
                        _send_reminder(db, dm, "24h")
                        dm.reminder_24h_sent = True
                        db.commit()
                if datetime.timedelta(minutes=settings.REMINDER_1H_MIN_MINUTES) < time_until < datetime.timedelta(minutes=settings.REMINDER_1H_MAX_MINUTES):
                    if not dm.reminder_1h_sent:
                        _send_reminder(db, dm, "1h")
                        dm.reminder_1h_sent = True
                        db.commit()
        except Exception as exc:
            logger.error(f"[SENTINEL] Meeting reminder failed for prospect {dm_id}: {exc}", exc_info=True)


def _send_reminder(db, dm, reminder_type: str):
    subject = f"Reminder: Discovery Call with {dm.campaign.user_intel.company_name} ({reminder_type} to go)"
    body = f"Hi {dm.name},\n\nThis is a quick reminder for our discovery call scheduled in {reminder_type}.\n\n"
    if dm.scheduling_note and "Conflict" in dm.scheduling_note:
        body += f"Note: {dm.scheduling_note}\n\n"
    body += f"Meeting Link: {dm.meeting_link}\n\nLooking forward to it!"
    creds = TokenService.get_google_credentials(db, dm.campaign.user_id)
    email_service.send_email(
        dm.email, subject, body, creds=creds,
        thread_id=dm.thread_id, in_reply_to_message_id=dm.last_rfc_message_id,
    )
    logger.info(f"[SENTINEL] {reminder_type} Reminder deployed for stakeholder {dm.name}")

