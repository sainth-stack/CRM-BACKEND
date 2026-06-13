"""
Phase 1: Parallel Background Pipeline
Triggers Track A (CSV) and Track B (User Intel) concurrently.
"""
from app.db import models
from sqlalchemy.orm import Session
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
from app.workers.utils import with_short_lived_db_session, execute_with_db_session, db_session
import json
import datetime
from datetime import UTC
import asyncio
from app.core.security import acquire_lock, release_lock


@celery_app.task(name="app.workers.tasks.intel_worker.process_csv_worker", bind=True, max_retries=3, default_retry_delay=60)
@with_short_lived_db_session
def process_csv_worker(self, db: Session, campaign_id: str):
    """
    Track A: Heavy Data Lifting (CSV Ingestion & Trimming)
    Pulls from the persistent SSoT artifact in local/cloud storage.
    """
    from app.services.campaign_service import campaign_service
    from app.core.security import acquire_lock, release_lock
    import os

    lock_key = f"campaign_csv:{campaign_id}"
    if not acquire_lock(lock_key, ttl=600):
         logger.warning(f"⚠️  [Track A] Could not acquire lock for {campaign_id}. Already locked?")
         return

    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.error(f"❌ [Track A] Campaign {campaign_id} not found in DB.")
            return

        logger.info(f"🚀 [Track A] Mobilizing SSoT Ingestion from DB for {campaign_id} (Status: {campaign.status})")

        # Normalize the trimmed CSV ONCE into campaign_leads rows (typed columns),
        # then drop the blob: the structured rows are now the SSoT and downstream
        # stages read them in chunks instead of re-parsing the CSV. Idempotent —
        # a retry that finds leads already present skips re-parsing.
        from app.services import lead_repository
        csv_content = campaign.trimmed_csv_data
        try:
            if csv_content and not lead_repository.has_leads(db, campaign_id):
                lead_repository.normalize_csv_to_leads(db, campaign_id, csv_content)
            # Free the blob once the rows exist (csv_done now derives from leads).
            if lead_repository.has_leads(db, campaign_id) and campaign.trimmed_csv_data is not None:
                campaign.trimmed_csv_data = None
            db.commit()
        except Exception as norm_err:
            db.rollback()
            logger.error(f"❌ [Track A] Lead normalization failed for {campaign_id}: {norm_err}. "
                         f"Keeping blob as fallback.")

        # Execute the Indestructible State-Machine Orchestrator
        campaign_service.process_state_machine(db, campaign_id)
    finally:
        release_lock(lock_key)

@celery_app.task(name="app.workers.tasks.intel_worker.research_user_company_worker", bind=True, max_retries=3, default_retry_delay=120)
@with_short_lived_db_session
def research_user_company_worker(self, db: Session, campaign_id: str):
    """
    Track B: Brand Intelligence Agent (User Intel)
    """
    from app.services.user_intel_service import UserIntelService
    from app.core.security import acquire_lock, release_lock
    from app.services.campaign_service import campaign_service
    
    lock_key = f"campaign_intel:{campaign_id}"
    if not acquire_lock(lock_key, ttl=900):
        logger.warning(f"⚠️  [Track B] Research already in progress for {campaign_id}. Skipping.")
        return

    # 1. Short read transaction to fetch input variables
    with db_session() as db:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            release_lock(lock_key)
            return
        intel = campaign.user_intel
        if not intel:
            logger.error(f"❌ [Track B] No UserIntel record for {campaign_id}")
            release_lock(lock_key)
            return
        website = intel.website
        prompt = campaign.prompt

    logger.info(f"🧠 [Track B] Building Brand Brain for: {website}")
    user_intel_svc = UserIntelService()
    
    # 2. Heavy LLM research task is run holding ZERO database sessions
    try:
        research_data = asyncio.run(user_intel_svc.research_user_company(website, prompt))
    except Exception as e:
        logger.error(f"Track B External Research Failure: {e}")
        release_lock(lock_key)
        self.retry(exc=e)
        return

    # 3. Short write transaction to persist research findings
    with db_session() as db:
        try:
            campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            intel = campaign.user_intel
            
            if research_data and intel and campaign:
                intel.company_name = research_data.get("exact_company_name")
                intel.website = research_data.get("website")
                intel.motto = research_data.get("motto")
                intel.offerings = json.dumps(research_data.get("core_offerings"))
                intel.deep_research = research_data.get("deep_research")
                intel.target_customers = research_data.get("target_customers")
                intel.competitive_advantages = research_data.get("competitive_advantages")
                intel.proof_points = research_data.get("proof_points")
                intel.capability_to_pain_map = research_data.get("capability_to_pain_map")
                intel.v2_intel = research_data
                if campaign.status in [models.CampaignStatus.PENDING, models.CampaignStatus.STAGE_1_CSV_TRIMMED]:
                    campaign.status = models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE
                db.commit()
                logger.info(f"✅ [Track B] Brand Intelligence Secured for {campaign_id}. Advancing State Machine.")
                campaign_service.process_state_machine(db, campaign_id)
            release_lock(lock_key)
        except Exception as e:
            logger.error(f"Track B DB Write Failure: {e}")
            release_lock(lock_key)
            self.retry(exc=e)

# NOTE: Input validation is no longer a background track. It runs synchronously
# in the API (POST /campaigns and PATCH /campaigns/{id}) via the single
# InputValidationService, which persists corrections and writes the review that
# satisfies the pipeline's val_done gate. The former Track C worker
# (validate_input_worker -> CampaignValidator) has been removed.
