import re
import datetime
from datetime import UTC
from sqlalchemy.orm import Session
from app.db import models
from app.agents.intent_classifier import classify_reply_intent, extract_schedule_info

# Matches the standard "On <date>, <name> <email> wrote:" quoted-thread delimiter.
# Covers Gmail, Outlook, Apple Mail, and Thunderbird formatting variants.
_QUOTED_THREAD_RE = re.compile(
    r'\n[ \t]*On\s.+?wrote:\s*$',
    re.DOTALL | re.MULTILINE,
)
# Also covers Outlook-style delimiters
_OUTLOOK_DELIMITER_RE = re.compile(
    r'\n\s*-{3,}\s*(Original Message|Forwarded Message|From:)',
    re.IGNORECASE,
)

def strip_quoted_reply(body: str) -> str:
    """
    Strips the quoted original-thread block from an email reply body,
    leaving only the prospect's own top-level text.
    Handles Gmail, Outlook, Apple Mail, and Thunderbird quoting styles.
    """
    if not body:
        return body
    # Remove "> " style quoted lines (RFC 2822 inline quoting)
    lines = body.splitlines()
    top_lines = []
    for line in lines:
        if line.startswith('>'):
            break
        top_lines.append(line)
    cleaned = "\n".join(top_lines)
    # Remove "On <date> ... wrote:" block and everything after it
    cleaned = _QUOTED_THREAD_RE.split(cleaned)[0]
    cleaned = _OUTLOOK_DELIMITER_RE.split(cleaned)[0]
    return cleaned.strip()

class InboxService:
    """
    Handles reply matching, intent classification, and 
    scheduling coordination for incoming prospect communications.
    """
    
    @staticmethod
    def handle_prospect_reply(db: Session, dm: models.DecisionMaker, reply: dict):
        """
        Processes a single incoming reply.
        Records communication logs, classifies intent, and triggers downstream transitions.
        """
        from app.services.outreach_service import outreach_service
        from app.workers.lifecycle import utcnow_naive
        from app.workers.discovery_scheduling import _process_booking, _request_scheduling_clarification

        reply_received_at = reply.get("received_at") or utcnow_naive()
        
        # 1. Update State Anchors
        dm.last_reply_at = reply_received_at
        dm.reminder_count = 0
        dm.termination_reason = None
        dm.retry_after = None

        # 2. Extract History for AI Context
        last_sent = (
            db.query(models.CommunicationLog)
            .filter(
                models.CommunicationLog.dm_id == dm.id,
                models.CommunicationLog.direction == "SENT",
            )
            .order_by(models.CommunicationLog.received_at.desc())
            .first()
        )

        # 3. Intent Classification — strip quoted thread so the classifier
        #    only sees the prospect's own words, not forwarded date/time headers.
        clean_reply = strip_quoted_reply(reply["body"])
        classification = classify_reply_intent(
            last_sent.body if last_sent else "",
            clean_reply,
        )
        intent = classification["intent"]
        dm.reply_intent = intent
        dm.intent_last = intent

        # 4. Persistence
        db.add(
            models.CommunicationLog(
                campaign_id=dm.campaign_id,
                dm_id=dm.id,
                direction="RECEIVED",
                subject=reply["subject"],
                body=reply["body"],
                message_id=reply["message_id"],
                received_at=reply_received_at,
            )
        )
        db.flush()

        # 5. Transition Coordination
        if intent == "BOOKING":
            today_str = datetime.datetime.now(UTC).strftime("%Y-%m-%d")
            extract = extract_schedule_info(
                clean_reply,
                today_str,
                dm.target_company.location if dm.target_company else "Global",
            )
            
            if extract and extract.get("date") and extract.get("time"):
                _process_booking(db, dm, extract)
            else:
                _request_scheduling_clarification(db, dm, source="missing_date_or_time")
        else:
            outreach_service.process_intent_transition(db, dm, intent)

inbox_service = InboxService()
