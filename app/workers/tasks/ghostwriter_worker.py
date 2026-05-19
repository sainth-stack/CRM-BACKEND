from app.db.database import SessionLocal
from app.db import models
from app.agents.email_drafter import draft_personalized_email, draft_followup_email, draft_nudge_email, draft_discovery_request
from app.services.drafting_service import DraftingService
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
from sqlalchemy.exc import IntegrityError
import json
import datetime
import gc
from datetime import UTC
from app.workers.lifecycle import transition_prospect
from app.workers.utils import heartbeat_lease


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


from app.services.observability_service import ObservabilityService
from sqlalchemy.orm import joinedload

def _save_drafts_batch(campaign_id: str, drafts_batch: list):
    """
    Issue 1: Batch-commits a collection of drafts to the database at once.
    This significantly reduces transaction overhead, locks, and connection counts.
    """
    db = SessionLocal()
    try:
        for dm_id, draft_set in drafts_batch:
            dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
            if dm:
                primary_variant = draft_set.variants.get("primary", list(draft_set.variants.values())[0])
                new_draft = models.EmailDraft(
                    campaign_id=campaign_id,
                    decision_maker_id=dm.id,
                    subject=primary_variant.get("subject"),
                    body=primary_variant.get("body"),
                    status="DRAFTED",
                    followup_index=0,
                    draft_type="INITIAL",
                    variants=draft_set.variants,
                    strategic_observation=draft_set.strategic_observation,
                    pain_hypothesis=draft_set.pain_hypothesis,
                    personalization_hook=draft_set.personalization_hook
                )
                db.add(new_draft)
                transition_prospect(
                    db,
                    dm,
                    state=models.ProspectState.DRAFTED,
                    status="DRAFTED",
                    reason="INITIAL_DRAFTED",
                    actor="ghostwriter",
                    metadata={"draft_type": "INITIAL", "v2_enhanced": True},
                )
        db.commit()
        logger.info(f"[BATCH] Committed batch of {len(drafts_batch)} drafts.")
    except Exception as e:
        db.rollback()
        logger.error(f"[BATCH] Failed to commit drafts batch: {e}")
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
@ObservabilityService.track_celery_task("draft_emails_worker")
def draft_emails_worker(self, campaign_id: str):
    """
    Phase 3: Personalization and drafting worker.
    Generates personalized outreach content for all stakeholders identified in previous phases.
    """
    worker_id = self.request.id
    from app.core.security import acquire_lock, release_lock
    
    lock_key = f"campaign_stage6:{campaign_id}"
    if not acquire_lock(lock_key, ttl=3600): return

    logger.info(f"[CAMPAIGN] Initiating draft generation for campaign {campaign_id}")
    
    # 1. Fetch active target list in a paginated, short read session
    db = SessionLocal()
    dm_data_list = []
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign or not campaign.user_intel:
            release_lock(lock_key)
            return

        # Temporal Boundary Check: Security gate for trial accounts
        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                logger.info(f"[CAMPAIGN] Campaign {campaign_id} trial expired.")
                release_lock(lock_key)
                return

        # Issue 5: Paginate dataset queries to keep memory utilization flat
        PAGE_SIZE = 500
        offset = 0
        while True:
            dms_page = (db.query(models.DecisionMaker)
                        .options(joinedload(models.DecisionMaker.target_company))
                        .filter(models.DecisionMaker.campaign_id == campaign_id)
                        .order_by(models.DecisionMaker.id)
                        .limit(PAGE_SIZE)
                        .offset(offset)
                        .all())
            if not dms_page:
                break
            
            for dm in dms_page:
                # Issue 5: Filter out pre-drafted items early to avoid massive object overhead
                exists = db.query(models.EmailDraft.id).filter(models.EmailDraft.decision_maker_id == dm.id).first() is not None
                if not exists:
                    dm_data_list.append({
                        "id": dm.id,
                        "name": dm.name,
                        "target_company_id": dm.target_company_id,
                        "target_company_name": dm.target_company.name if dm.target_company else None
                    })
            offset += PAGE_SIZE
    finally:
        db.close()

    logger.info(f"[GHOSTWRITER] Generating content for {len(dm_data_list)} prospects.")
    drafter = DraftingService()
    processed_count = 0
    BATCH_SIZE = 10
    pending_drafts = []

    for item in dm_data_list:
        processed_count += 1
        dm_id = item["id"]
        dm_name = item["name"]
        company_id = item["target_company_id"]
        company_name = item["target_company_name"]

        # Heartbeat Pulse inside the loop (session-safe)
        if processed_count % 5 == 0:
            db_heartbeat = SessionLocal()
            try:
                heartbeat_lease(db_heartbeat, campaign_id, worker_id)
            except Exception:
                pass
            finally:
                db_heartbeat.close()

        # V2 Strategic Drafting Cluster runs with ZERO database sessions held
        try:
            draft_set = drafter.generate_draft_set(None, dm_id)
            
            if draft_set and draft_set.variants:
                pending_drafts.append((dm_id, draft_set))
                
                # Check if we should commit the accumulated batch
                if len(pending_drafts) >= BATCH_SIZE:
                    _save_drafts_batch(campaign_id, pending_drafts)
                    pending_drafts.clear()
            else:
                logger.warning(f"[GHOSTWRITER] Null Draft Set for {dm_name}")
        except Exception as draft_e:
            logger.error(f"Drafting Failure for {dm_name}: {draft_e}")
            continue

    # Commit any remaining drafts in the final batch
    if pending_drafts:
        _save_drafts_batch(campaign_id, pending_drafts)

    # Final Campaign Status Update in a short write session
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if campaign:
            expected_count = db.query(models.DecisionMaker.id).filter(models.DecisionMaker.campaign_id == campaign_id).count()
            drafted_count = db.query(models.EmailDraft.id).filter(models.EmailDraft.campaign_id == campaign_id).count()

            if expected_count == 0:
                campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
                campaign.status_reason = "Mission stalled: Zero stakeholders identified during previous phases."
                logger.warning(f"[CAMPAIGN] Campaign {campaign_id} stalled: No stakeholders identified.")
            elif drafted_count >= expected_count:
                campaign.status = models.CampaignStatus.COMPLETED
                campaign.status_reason = f"Mission successful: {drafted_count}/{expected_count} drafts secured."
                logger.info(f"[CAMPAIGN] Success: Generated {drafted_count}/{expected_count} drafts for {campaign_id}.")
            elif drafted_count > 0:
                campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
                campaign.status_reason = f"Partial Success: Only {drafted_count}/{expected_count} drafts could be generated."
                logger.warning(f"[CAMPAIGN] Partial Success: Generated {drafted_count}/{expected_count} drafts for {campaign_id}.")
            else:
                campaign.status = models.CampaignStatus.FAILED
                campaign.status_reason = "Mission Failure: Zero drafts could be generated despite identified stakeholders."
                logger.error(f"[CAMPAIGN] Mission Failure: 0/{expected_count} drafts generated for {campaign_id}.")
            db.commit()
    finally:
        db.close()
        release_lock(lock_key)


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
