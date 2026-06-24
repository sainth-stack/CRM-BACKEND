from sqlalchemy.orm import Session
from app.db import models
from app.core.logging_config import logger
from app.workers.lifecycle import (
    hold_company_siblings,
    restore_held_company_siblings,
    terminate_prospect,
    transition_prospect,
    utcnow_naive
)

class OutreachService:
    """
    Handles prospect lifecycle management, state transitions, 
    and multi-stakeholder coordination.
    """
    
    @staticmethod
    def process_intent_transition(db: Session, dm: models.DecisionMaker, intent: str):
        """
        Manages the transition of a prospect based on classified intent.
        Coordinates sibling locks and CRM status updates.
        """
        from app.workers.tasks.ghostwriter_worker import draft_discovery_worker, draft_followup_worker

        if intent == "POSITIVE":
            # Ignore if already booked
            if dm.state == models.ProspectState.MEETING_BOOKED:
                return

            logger.info(f"[OUTREACH] Positive: {dm.name} (Transitioning to Discovery)")
            dm.termination_reason = None
            dm.retry_after = None

            if dm.target_company:
                dm.target_company.status = "DISCOVERY_CALL"

            # Case: Already in a discovery/waiting state, just update status
            if dm.state in [
                models.ProspectState.DISCOVERY_CALL,
                models.ProspectState.DISCOVERY_EXPIRED,
            ]:
                if dm.target_company:
                    for sibling in hold_company_siblings(db, dm):
                        logger.debug(f"[OUTREACH] Holding concurrent stakeholder {sibling.name}")

                transition_prospect(
                    db,
                    dm,
                    state=models.ProspectState.DISCOVERY_CALL,
                    status="DISCOVERY_CALL",
                    reason="POSITIVE_REPLY",
                    actor="service",
                    metadata={"intent": intent, "source_state": dm.state.value if dm.state else None},
                )
                dm.next_action_at = None
                draft_followup_worker(dm.id, db=db, manual_scheduling=True)
                return

            # Case: Fresh positive intent from outbound
            transition_prospect(
                db,
                dm,
                state=models.ProspectState.DISCOVERY_CALL,
                status="DISCOVERY_CALL",
                reason="POSITIVE_REPLY",
                actor="service",
                metadata={"intent": intent},
            )
            dm.next_action_at = None
            
            if dm.target_company:
                for sibling in hold_company_siblings(db, dm):
                    logger.debug(f"[OUTREACH] Holding concurrent stakeholder {sibling.name}")

            draft_discovery_worker(dm.id, db=db)

        elif intent == "NEGATIVE":
            logger.info(f"[OUTREACH] Rejected: {dm.name} (Terminating)")
            restore_held_company_siblings(db, dm)

            terminate_prospect(
                db,
                dm,
                models.ProspectTerminationReason.NEGATIVE_REPLY,
                retryable=True,
                actor="service",
                metadata={"intent": intent},
            )

        elif intent == "NEUTRAL":
            if dm.followup_count < 2:
                logger.info(f"[OUTREACH] Neutral: {dm.name} (Routing to Follow-up Drafting)")
                transition_prospect(
                    db,
                    dm,
                    state=models.ProspectState.NEUTRAL,
                    status="NEUTRAL",
                    reason="NEUTRAL_REPLY",
                    actor="service",
                    metadata={"intent": intent},
                )
                dm.next_action_at = utcnow_naive()
                dm.termination_reason = None
                dm.retry_after = None
                draft_followup_worker(dm.id, db=db)
            else:
                logger.info(f"[OUTREACH] Neutral: {dm.name} (Threshold Exhausted - Terminating)")
                restore_held_company_siblings(db, dm)

                terminate_prospect(
                    db,
                    dm,
                    models.ProspectTerminationReason.FOLLOWUP_EXHAUSTED,
                    retryable=True,
                    actor="service",
                    metadata={"intent": intent},
                )

outreach_service = OutreachService()
