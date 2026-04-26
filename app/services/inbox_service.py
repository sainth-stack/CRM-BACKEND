from sqlalchemy.orm import Session
from app.db import models
from app.core.logging_config import logger
from app.agents.intent_classifier import classify_reply_intent
from app.agents.discovery_agent import extract_schedule_info
from app.workers.lifecycle import utcnow_naive
from app.workers.discovery_scheduling import _process_booking, _request_scheduling_clarification
from app.services.outreach_service import outreach_service
import datetime
from datetime import UTC

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

        # 3. Intent Classification
        classification = classify_reply_intent(
            last_sent.body if last_sent else "",
            reply["body"],
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
        # Handle Discovery/Scheduling path vs. Standard Outreach path
        if dm.state in [
            models.ProspectState.DISCOVERY_CALL,
            models.ProspectState.WAITING_FOR_REPLY,
            models.ProspectState.DISCOVERY_EXPIRED,
        ]:
            today_str = datetime.datetime.now(UTC).strftime("%Y-%m-%d")
            extract = extract_schedule_info(
                reply["body"],
                today_str,
                dm.target_company.location if dm.target_company else "Global",
            )
            
            if intent == "NEGATIVE":
                outreach_service.process_intent_transition(db, dm, intent)
            elif extract and extract.get("date") and extract.get("time"):
                _process_booking(db, dm, extract)
            else:
                _request_scheduling_clarification(db, dm, source="missing_date_or_time")
        else:
            outreach_service.process_intent_transition(db, dm, intent)

inbox_service = InboxService()
