"""
Sentinel Workers: Inbox Polling + Inactivity Nudges + Meeting Reminders
Background cron-style tasks that run on a schedule via APScheduler.
"""
from app.db.database import SessionLocal
from app.db import models
from app.agents.intent_classifier import classify_reply_intent
from app.agents.discovery_agent import extract_schedule_info, draft_discovery_request
from app.integrations.hubspot import hubspot_provider
from app.integrations.gmail import GmailProvider
from app.integrations.cal import cal_provider
from app.core.email_service import email_service
from app.core.token_service import TokenService
import datetime
import pytz
import re
from datetime import UTC
from app.workers.config.celery_app import celery_app


def poll_inbox_task(user_id: str):
    """Background Sentinel: Polls for replies for a SPECIFIC USER sector."""
    print(f"[SENTINEL] Scanning inbox for user sector {user_id}...")
    db = SessionLocal()
    try:
        creds = TokenService.get_google_credentials(db, user_id)
        if not creds:
            print(f"[SENTINEL] Aborting: No outreach capability established for user {user_id}.")
            return

        provider = GmailProvider(creds)
        replies = provider.get_latest_replies()
        for reply in replies:
            dm = None
            in_reply_to = (reply.get("in_reply_to") or "").strip()

            if not dm and reply.get("thread_id"):
                dm = db.query(models.DecisionMaker).join(models.Campaign).filter(
                    models.DecisionMaker.thread_id == reply["thread_id"],
                    models.Campaign.user_id == user_id
                ).first()

            if not dm:
                email_match = re.search(r'[\w\.-]+@[\w\.-]+', reply.get("from", ""))
                if email_match:
                    clean_email = email_match.group(0).lower()
                    dm = db.query(models.DecisionMaker).join(models.Campaign).filter(
                        models.DecisionMaker.email == clean_email,
                        models.Campaign.user_id == user_id
                    ).first()
                    if dm and not dm.thread_id:
                        dm.thread_id = reply.get("thread_id")
                        print(f"[SENTINEL] Active mission link established via legacy coordinate for {dm.name}")

            if dm:
                existing_log = db.query(models.CommunicationLog).filter(
                    models.CommunicationLog.message_id == reply.get("message_id"),
                    models.CommunicationLog.direction == "RECEIVED"
                ).first()
                if existing_log:
                    continue

                print(f"[SENTINEL] Match Found: {dm.name} from {dm.target_company.name}")

                last_sent = db.query(models.CommunicationLog).filter(
                    models.CommunicationLog.dm_id == dm.id,
                    models.CommunicationLog.direction == "SENT"
                ).order_by(models.CommunicationLog.received_at.desc()).first()

                original_text = last_sent.body if last_sent else "Initial context missing."

                classification = classify_reply_intent(original_text, reply["body"])
                intent = classification["intent"]
                reason = classification["reasoning"]
                dm.reply_intent = intent
                print(f"[SENTINEL] Intent for {dm.name}: {intent} ({reason})")

                new_log = models.CommunicationLog(
                    campaign_id=dm.campaign_id,
                    dm_id=dm.id,
                    direction="RECEIVED",
                    subject=reply["subject"],
                    body=reply["body"],
                    message_id=reply["message_id"]
                )
                db.add(new_log)
                db.flush()

                if dm.status in ["DISCOVERY_CALL", "WAITING_FOR_REPLY"]:
                    print(f"[DISCOVERY] Extracting coordinates from {dm.name}'s reply...")
                    today_str = datetime.datetime.now(UTC).strftime("%Y-%m-%d")
                    extract = extract_schedule_info(
                        reply["body"], today_str,
                        dm.target_company.location if dm.target_company else "Global"
                    )

                    if extract and extract.get("date") and extract.get("time"):
                        raw_tz = (extract.get("timezone") or "IST").upper()
                        print(f"[DISCOVERY] Extracted Coordinate: {extract['date']} @ {extract['time']} {raw_tz}")
                        try:
                            TZ_MAP = {
                                "IST": "Asia/Kolkata", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
                                "EST": "America/New_York", "EDT": "America/New_York", "CST": "America/Chicago",
                                "CDT": "America/Chicago", "MST": "America/Denver", "MDT": "America/Denver",
                                "GMT": "UTC", "UTC": "UTC", "BST": "Europe/London", "CET": "Europe/Paris"
                            }
                            source_tz_str = TZ_MAP.get(raw_tz, "Asia/Kolkata")
                            source_tz = pytz.timezone(source_tz_str)
                            naive_dt = datetime.datetime.strptime(f"{extract['date']} {extract['time']}", "%Y-%m-%d %H:%M")
                            localized_dt = source_tz.localize(naive_dt)
                            utc_dt = localized_dt.astimezone(pytz.UTC)
                            utc_iso = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                            print(f"[DISCOVERY] Dispatching Cal.com Reservation: {utc_iso}")
                            booking = cal_provider.book_meeting(email=dm.email, name=dm.name, start_time=utc_iso)

                            if booking:
                                dm.status = "MEETING_BOOKED"
                                dm.meeting_link = booking["link"]
                                ist_tz = pytz.timezone("Asia/Kolkata")
                                ist_dt = utc_dt.astimezone(ist_tz)
                                dm.scheduled_time = ist_dt.replace(tzinfo=None)
                                dm.timezone = "IST"

                                target_co = dm.target_company
                                if target_co: target_co.status = "MEETING_BOOKED"
                                others = db.query(models.DecisionMaker).filter(
                                    models.DecisionMaker.target_company_id == target_co.id,
                                    models.DecisionMaker.id != dm.id
                                ).all()
                                for other in others:
                                    other.status = "TERMINATED"
                                    hubspot_provider.update_lead_status(other.hubspot_id, "Terminated (Internal Lead Secured)")

                                hubspot_provider.update_lead_status(dm.hubspot_id, f"Meeting Booked: {dm.scheduled_time} IST")
                                print(f"[DISCOVERY] SUCCESS: Secured meeting for {dm.name} at {dm.meeting_link}")

                                confirmation = draft_discovery_request(
                                    user_intel={
                                        "name": dm.campaign.user_intel.company_name,
                                        "offerings": dm.campaign.user_intel.offerings,
                                        "deep_research": dm.campaign.user_intel.deep_research
                                    },
                                    dm_name=dm.name,
                                    dm_position=dm.position,
                                    target_company=dm.target_company.name,
                                    last_interest=reply["body"],
                                    booked_link=dm.meeting_link
                                )
                                if confirmation:
                                    creds = TokenService.get_google_credentials(db, dm.campaign.user_id)
                                    msg_data = email_service.send_email(
                                        to_email=dm.email,
                                        subject=confirmation["subject"],
                                        body=confirmation["body"],
                                        creds=creds,
                                        thread_id=dm.thread_id
                                    )
                                    dm.last_message_id = msg_data["id"]
                                    dm.thread_id = msg_data["thread_id"]
                                    print(f"[DISCOVERY] Confirmation deployed to {dm.name}.")
                            else:
                                print(f"[DISCOVERY] Booking failed. Potential conflict or invalid slot.")
                        except Exception as e:
                            print(f"[DISCOVERY] Booking Engine Failure: {e}")
                    else:
                        print(f"[DISCOVERY] Extraction failed. Awaiting human-in-the-loop coordination.")
                else:
                    process_intent_transition(db, dm, intent)

        db.commit()
    except Exception as e:
        print(f"[SENTINEL] Operational Error: {e}")
        db.rollback()
    finally:
        db.close()


def process_intent_transition(db, dm, intent):
    """Executes the business logic of Phase 2 transitions."""
    from app.workers.tasks.ghostwriter_worker import draft_followup_worker, draft_discovery_worker

    if intent == "POSITIVE":
        if dm.status not in ["MEETING_BOOKED", "DISCOVERY_CALL", "WAITING_FOR_REPLY"]:
            print(f"[DISCOVERY] Positive intent detected for {dm.name}. Initiating Inquiry Draft & Company Lock...")
            dm.status = "DISCOVERY_CALL"
            target_co = dm.target_company
            if target_co:
                target_co.status = "DISCOVERY_CALL"
                others = db.query(models.DecisionMaker).filter(
                    models.DecisionMaker.target_company_id == target_co.id,
                    models.DecisionMaker.id != dm.id
                ).all()
                for other in others:
                    if other.status not in ["TERMINATED", "MEETING_BOOKED"]:
                        print(f"[DISCOVERY] Suppressing internal competitor: {other.name}")
                        other.status = "TERMINATED"
                        hubspot_provider.update_lead_status(other.hubspot_id, "Terminated (Internal Lead Secured)")
            draft_discovery_worker(dm.id, db=db)
    elif intent == "NEGATIVE":
        dm.status = "TERMINATED"
        hubspot_provider.update_lead_status(dm.hubspot_id, "Terminated")
    elif intent == "NEUTRAL":
        if dm.followup_count < 11:
            draft_followup_worker(dm.id)
        else:
            dm.status = "TERMINATED"
            hubspot_provider.update_lead_status(dm.hubspot_id, "Terminated (Exhausted 11 Follow-ups)")


@celery_app.task
def poll_all_users_task():
    """Governing task: mobilizing inbox sentinel for all user sectors."""
    db = SessionLocal()
    try:
        users = db.query(models.User).all()
        for user in users:
            if user.is_demo and user.demo_expires_at:
                if user.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                    continue
            poll_inbox_task(user.id)
    finally:
        db.close()


def check_upcoming_meetings_task():
    """Sentinel for meeting reminders."""
    db = SessionLocal()
    try:
        now = datetime.datetime.now(UTC)
        booked_dms = db.query(models.DecisionMaker).filter(models.DecisionMaker.status == "MEETING_BOOKED").all()
        for dm in booked_dms:
            if not dm.scheduled_time: continue
            meeting_time = dm.scheduled_time.replace(tzinfo=UTC)
            time_until = meeting_time - now
            if datetime.timedelta(hours=22) < time_until < datetime.timedelta(hours=25):
                if not dm.reminder_24h_sent:
                    _send_reminder(db, dm, "24h")
                    dm.reminder_24h_sent = True
                    db.commit()
            if datetime.timedelta(minutes=45) < time_until < datetime.timedelta(minutes=75):
                if not dm.reminder_1h_sent:
                    _send_reminder(db, dm, "1h")
                    dm.reminder_1h_sent = True
                    db.commit()
    finally:
        db.close()


def _send_reminder(db, dm, reminder_type: str):
    """Dispatches a meeting reminder email."""
    subject = f"Reminder: Discovery Call with {dm.campaign.user_intel.company_name} ({reminder_type} to go)"
    body = f"Hi {dm.name},\n\nThis is a quick reminder for our discovery call scheduled in {reminder_type}.\n\n"
    if dm.scheduling_note and "Conflict" in dm.scheduling_note:
        body += f"Note: {dm.scheduling_note}\n\n"
    body += f"Meeting Link: {dm.meeting_link}\n\nLooking forward to it!"
    creds = TokenService.get_google_credentials(db, dm.campaign.user_id)
    email_service.send_email(dm.email, subject, body, creds=creds, thread_id=dm.thread_id)
    hubspot_provider.update_lead_status(dm.hubspot_id, f"{reminder_type} Reminder Sent")
    print(f"[SENTINEL] {reminder_type} Reminder sent to {dm.name}")


def check_inactivity_reminders_task():
    """Silence Sentinel: Checks for non-responsive prospects and triggers nudges."""
    print("[SENTINEL] Auditing prospect silence levels...")
    db = SessionLocal()
    try:
        from datetime import timedelta
        now = datetime.datetime.now(UTC)
        threshold = timedelta(days=2)
        active_states = ["INITIAL_SENT", "REMINDER_1_SENT", "REMINDER_2_SENT", "WAITING_FOR_REPLY"]
        prospects = db.query(models.DecisionMaker).filter(
            (models.DecisionMaker.status.in_(active_states)) |
            (models.DecisionMaker.status.contains("FOLLOWUP_"))
        ).all()

        for dm in prospects:
            last_received = db.query(models.CommunicationLog).filter(
                models.CommunicationLog.dm_id == dm.id,
                models.CommunicationLog.direction == "RECEIVED"
            ).order_by(models.CommunicationLog.received_at.desc()).first()

            last_sent = db.query(models.CommunicationLog).filter(
                models.CommunicationLog.dm_id == dm.id,
                models.CommunicationLog.direction == "SENT"
            ).order_by(models.CommunicationLog.received_at.desc()).first()

            if not last_sent: continue
            if last_received and last_received.received_at > last_sent.received_at:
                continue

            time_since_last_sent = now - last_sent.received_at.replace(tzinfo=UTC)
            if time_since_last_sent > threshold:
                _process_inactivity_transition(db, dm, last_sent)

        db.commit()
    except Exception as e:
        print(f"[SENTINEL] Inactivity Audit Error: {e}")
        db.rollback()
    finally:
        db.close()


def _process_inactivity_transition(db, dm, last_sent_log):
    """Executes the automated reminder sequence."""
    current_status = dm.status
    target_status = None
    hs_label = None

    if current_status in ["INITIAL_SENT", "WAITING_FOR_REPLY"] or current_status.startswith("FOLLOWUP_"):
        target_status = "REMINDER_1_SENT"
        hs_label = "Reminder 1 Sent"
    elif current_status == "REMINDER_1_SENT":
        target_status = "REMINDER_2_SENT"
        hs_label = "Reminder 2 Sent"
    elif current_status == "REMINDER_2_SENT":
        dm.status = "TERMINATED"
        hubspot_provider.update_lead_status(dm.hubspot_id, "Terminated (No Reply)")
        print(f"[SENTINEL] Terminating silent prospect: {dm.name}")
        return

    if target_status:
        try:
            from app.agents.email_drafter import draft_nudge_email
            body = draft_nudge_email(dm.name, dm.campaign.user_intel.company_name)
            subject = f"Re: {last_sent_log.subject}"
            creds = TokenService.get_google_credentials(db, dm.campaign.user_id)
            msg_data = email_service.send_email(
                to_email=dm.email, subject=subject, body=body,
                creds=creds, thread_id=dm.thread_id
            )
            new_log = models.CommunicationLog(
                campaign_id=dm.campaign_id, dm_id=dm.id,
                direction="SENT", subject=subject, body=body,
                message_id=msg_data["id"]
            )
            db.add(new_log)
            dm.status = target_status
            dm.last_message_id = msg_data["id"]
            dm.thread_id = msg_data["thread_id"]
            hubspot_provider.update_lead_status(dm.hubspot_id, hs_label)
            print(f"[SENTINEL] Silence broken: {hs_label} deployed to {dm.name}")
        except Exception as e:
            print(f"[SENTINEL] Failed to deploy nudge to {dm.name}: {e}")


@celery_app.task
def check_all_meetings_task():
    """Governing task: mobilizing meeting reminders for all user sectors."""
    check_upcoming_meetings_task()


@celery_app.task
def check_all_inactivity_task():
    """Governing task: mobilizing silence sentinel for all user sectors."""
    check_inactivity_reminders_task()
