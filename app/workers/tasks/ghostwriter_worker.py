from app.db.database import SessionLocal
from app.db import models
from app.agents.email_drafter import draft_personalized_email, draft_followup_email, draft_nudge_email
from app.agents.discovery_agent import draft_discovery_request
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
import json
import datetime
import gc
from datetime import UTC
from app.workers.lifecycle import transition_prospect


DISCOVERY_DRAFT_START_INDEX = 100


def _next_discovery_draft_index(db, dm_id: str) -> int:
    existing_indices = [
        draft.followup_index
        for draft in db.query(models.EmailDraft).filter(
            models.EmailDraft.decision_maker_id == dm_id,
            models.EmailDraft.draft_type == "DISCOVERY",
        ).all()
        if draft.followup_index is not None
    ]
    if not existing_indices:
        return DISCOVERY_DRAFT_START_INDEX
    return max(max(existing_indices), DISCOVERY_DRAFT_START_INDEX - 1) + 1


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def draft_emails_worker(self, campaign_id: str):
    """
    Phase 3: AI-powered ghostwriting cluster.
    Generates hyper-personalized outreach content for all stakeholders identified in previous phases.
    Uses deep research data to ensure relevance and high conversion rates.
    """
    db = SessionLocal()
    try:
        from app.workers.utils import acquire_lease, release_lease, heartbeat_lease
        worker_id = f"worker:{self.request.id}"
        if not acquire_lease(db, campaign_id, worker_id):
            return

        logger.info(f"[MISSION CONTROL] Transition: Initiating Ghostwriting Cluster for {campaign_id}")
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign or not campaign.user_intel:
            return

        # Temporal Boundary Check: Security gate for trial accounts
        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                logger.info(f"[MISSION CONTROL] Suspension: Campaign {campaign_id} owner trial expired.")
                return

        user_intel_raw = campaign.user_intel
        offerings = []
        try:
            offerings = json.loads(user_intel_raw.offerings)
            if not isinstance(offerings, list):
                offerings = [str(offerings)]
        except:
            offerings = [str(user_intel_raw.offerings)]

        user_intel = {
            "company_name": user_intel_raw.company_name,
            "moto": user_intel_raw.motto or "N/A",
            "offerings": offerings,
            "deep_research": user_intel_raw.deep_research
        }

        dms = db.query(models.DecisionMaker).filter(models.DecisionMaker.campaign_id == campaign_id).all()
        logger.info(f"[GHOSTWRITER] Generating personalized content for {len(dms)} validated stakeholders.")

        processed_count = 0
        for dm in dms:
            processed_count += 1
            # Heartbeat Pulse
            if processed_count % 5 == 0:
                heartbeat_lease(db, campaign_id, worker_id)

            # Skip if already drafted
            if db.query(models.EmailDraft).filter(models.EmailDraft.decision_maker_id == dm.id).first():
                continue

            target_co = db.query(models.TargetCompany).filter(models.TargetCompany.id == dm.target_company_id).first()
            if not target_co: continue

            try:
                draft_data = draft_personalized_email(
                    user_intel,
                    {"name": dm.name, "position": dm.position},
                    target_co.name,
                    target_co.deep_research
                )
                if draft_data:
                    from sqlalchemy.exc import IntegrityError
                    try:
                        new_draft = models.EmailDraft(
                            campaign_id=campaign_id,
                            decision_maker_id=dm.id,
                            subject=draft_data.get("subject"),
                            body=draft_data.get("body"),
                            status="DRAFTED",
                            followup_index=0,
                            draft_type="INITIAL"
                        )
                        db.add(new_draft)
                        transition_prospect(
                            db,
                            dm,
                            state=models.ProspectState.DRAFTED,
                            status="DRAFTED",
                            reason="INITIAL_DRAFTED",
                            actor="ghostwriter",
                            metadata={"draft_type": "INITIAL"},
                        )
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        logger.info(f"[IDEMPOTENCY] Benign Collision: Initial draft for DM {dm.id} already exists.")
                        continue
                else:
                    logger.warning(f"[GHOSTWRITER] Null Draft for {dm.name}")
            except Exception as draft_e:
                logger.error(f"Drafting Failure for {dm.name}: {draft_e}")
                db.rollback()

        # Honest Outcome Audit: High-Fidelity Classification
        final_dms = db.query(models.DecisionMaker).filter(models.DecisionMaker.campaign_id == campaign_id).all()
        drafted_count = db.query(models.EmailDraft).filter(models.EmailDraft.campaign_id == campaign_id).count()
        expected_count = len(final_dms)

        if expected_count == 0:
            campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
            campaign.status_reason = "Mission stalled: Zero stakeholders identified during previous phases."
            logger.warning(f"[MISSION CONTROL] Campaign {campaign_id} stalled: Zero stakeholders identified.")
        elif drafted_count >= expected_count:
            campaign.status = models.CampaignStatus.COMPLETED
            campaign.status_reason = f"Mission successful: {drafted_count}/{expected_count} drafts secured."
            logger.info(f"[MISSION CONTROL] SUCCESS: {drafted_count}/{expected_count} drafts secured for {campaign_id}.")
        elif drafted_count > 0:
            campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
            campaign.status_reason = f"Partial Success: Only {drafted_count}/{expected_count} drafts could be generated."
            logger.warning(f"[MISSION CONTROL] PARTIAL SUCCESS: {drafted_count}/{expected_count} drafts secured for {campaign_id}.")
        else:
            campaign.status = models.CampaignStatus.FAILED
            campaign.status_reason = "Mission Failure: Zero drafts could be generated despite identified stakeholders."
            logger.error(f"[MISSION CONTROL] MISSION FAILURE: 0/{expected_count} drafts secured for {campaign_id}.")
            
        db.commit()
        release_lease(db, campaign_id, worker_id)
    except Exception as e:
        logger.error(f"Critical error in Email Ghostwriter Cluster: {e}", exc_info=True)
        try: release_lease(db, campaign_id, f"worker:{self.request.id}")
        except: pass
        self.retry(exc=e)
    finally:
        db.close()


def draft_followup_worker(dm_id: str, db=None, manual_scheduling: bool = False):
    """
    Persistence Engine: Nudge Dispatcher.
    Also handles 'Manual Coordination' fallbacks when the auto-booking probe fails.
    """
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True
    try:
        dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
        if not dm: return

        campaign = dm.campaign
        user_intel = {
            "company_name": campaign.user_intel.company_name,
            "deep_research": campaign.user_intel.deep_research
        }

        logs = db.query(models.CommunicationLog).filter(
            models.CommunicationLog.dm_id == dm.id
        ).order_by(models.CommunicationLog.received_at.desc()).limit(5).all()

        history_text = "\n".join([f"{log.direction}: {log.body}" for log in logs])
        
        if not manual_scheduling:
            dm.followup_count += 1
            nudge_num = dm.followup_count
        else:
            nudge_num = 0 # Coordination request

        draft_data = draft_followup_email(
            user_intel=user_intel,
            dm_info={"name": dm.name},
            target_company_name=dm.target_company.name,
            thread_history=history_text,
            followup_number=nudge_num,
            manual_scheduling=manual_scheduling
        )

        if draft_data:
            from sqlalchemy.exc import IntegrityError
            try:
                draft_index = _next_discovery_draft_index(db, dm.id) if manual_scheduling else dm.followup_count
                new_draft = models.EmailDraft(
                    campaign_id=campaign.id,
                    decision_maker_id=dm.id,
                    subject=draft_data["subject"],
                    body=draft_data["body"],
                    status="DRAFTED",
                    followup_index=draft_index,
                    draft_type="FOLLOWUP" if not manual_scheduling else "DISCOVERY"
                )
                db.add(new_draft)
                
                if manual_scheduling:
                    transition_prospect(
                        db,
                        dm,
                        state=models.ProspectState.DISCOVERY_CALL,
                        status="COORDINATION_DRAFTED",
                        reason="DISCOVERY_DRAFTED",
                            actor="ghostwriter",
                            metadata={"draft_type": "DISCOVERY", "draft_index": draft_index},
                        )
                    dm.next_action_at = None
                else:
                    transition_prospect(
                        db,
                        dm,
                        state=models.ProspectState.FOLLOWUP_ACTIVE,
                        status=f"FOLLOWUP_{dm.followup_count}_DRAFTED",
                        reason="FOLLOWUP_DRAFTED",
                        actor="ghostwriter",
                        metadata={"draft_type": "FOLLOWUP", "followup_index": dm.followup_count},
                    )
                    dm.next_action_at = None

                dm.termination_reason = None
                dm.retry_after = None
                    
                db.commit()
                logger.info(f"[FOLLOW-UP] Persistence triggered for {dm.name} | Index: {dm.followup_count} | Mode: {'ManualCoord' if manual_scheduling else 'Nudge'}")
            except IntegrityError:
                db.rollback()
                logger.info(f"[IDEMPOTENCY] Benign Collision: Follow-up {dm.followup_count} for DM {dm.id} already exists.")
    finally:
        if should_close:
            db.close()


def draft_discovery_worker(dm_id: str, db=None, is_auto_booking: bool = False):
    """
    Discovery Protocol Engine.
    Drafts the formal discovery call request after successful interest classification or auto-booking coordination.
    """
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True
    try:
        dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
        if not dm: return

        campaign = dm.campaign
        user_intel_obj = campaign.user_intel
        offerings = []
        try:
            offerings = json.loads(user_intel_obj.offerings)
            if not isinstance(offerings, list):
                offerings = [str(offerings)]
        except:
            offerings = [str(user_intel_obj.offerings)]

        user_intel = {
            "name": user_intel_obj.company_name,
            "offerings": ", ".join(offerings) if offerings else "AI-driven professional solutions",
            "deep_research": user_intel_obj.deep_research
        }

        last_reply = db.query(models.CommunicationLog).filter(
            models.CommunicationLog.dm_id == dm.id,
            models.CommunicationLog.direction == "RECEIVED"
        ).order_by(models.CommunicationLog.received_at.desc()).first()

        draft = draft_discovery_request(
            user_intel=user_intel,
            dm_name=dm.name,
            dm_position=dm.position,
            target_company=dm.target_company.name,
            last_interest=last_reply.body if last_reply else "Interest in AI solutions",
            booked_link=dm.meeting_link if is_auto_booking else None
        )

        if draft:
            from sqlalchemy.exc import IntegrityError
            try:
                new_draft = models.EmailDraft(
                    campaign_id=campaign.id,
                    decision_maker_id=dm.id,
                    subject=draft["subject"],
                    body=draft["body"],
                    status="DRAFTED",
                    followup_index=_next_discovery_draft_index(db, dm.id),
                    draft_type="DISCOVERY"
                )
                db.add(new_draft)
                transition_prospect(
                    db,
                    dm,
                    state=models.ProspectState.DISCOVERY_CALL,
                    status="DISCOVERY_CALL",
                    reason="DISCOVERY_DRAFTED",
                    actor="ghostwriter",
                    metadata={"draft_type": "DISCOVERY", "is_auto_booking": is_auto_booking},
                )
                dm.next_action_at = None
                dm.termination_reason = None
                dm.retry_after = None
                if should_close:
                    db.commit()
                logger.info(f"[DISCOVERY] Draft persistent for {dm.name} | Protocol: DISCOVERY_CALL")
            except IntegrityError:
                if should_close: db.rollback()
                logger.info(f"[IDEMPOTENCY] Discovery draft already exists for {dm.name}")
    except Exception as e:
        if should_close: db.rollback()
        logger.error(f"Discovery Protocol Failure for DM {dm_id}: {e}", exc_info=True)
        raise e
    finally:
        if should_close:
            db.close()
