"""
Phase 1: Parallel Background Pipeline
Triggers Track A (CSV) and Track B (User Intel) concurrently.
"""
from app.db.database import SessionLocal
from app.db import models
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
import json
import datetime
from datetime import UTC
import asyncio
from app.core.security import acquire_lock, release_lock
from app.agents.campaign_validator import CampaignValidator


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_csv_worker(self, campaign_id: str):
    """
    Track A: Heavy Data Lifting (CSV Ingestion & Trimming)
    Pulls from the persistent SSoT artifact in local/cloud storage.
    """
    db = SessionLocal()
    try:
        from app.services.campaign_service import campaign_service
        from app.core.security import acquire_lock, release_lock
        import os
        
        lock_key = f"campaign_csv:{campaign_id}"
        if not acquire_lock(lock_key, ttl=600):
             logger.warning(f"⚠️  [Track A] Could not acquire lock for {campaign_id}. Already locked?")
             return
        
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.error(f"❌ [Track A] Campaign {campaign_id} not found in DB.")
            release_lock(lock_key)
            return

        logger.info(f"🚀 [Track A] Mobilizing SSoT Ingestion from DB for {campaign_id} (Status: {campaign.status})")
        
        csv_content = campaign.trimmed_csv_data
        
        # Execute the Indestructible State-Machine Orchestrator
        asyncio.run(campaign_service.process_state_machine(db, campaign_id, csv_content if csv_content else None))
        
        release_lock(lock_key)
    except Exception as e:
        logger.error(f"Track A Failure: {e}")
        release_lock(f"campaign_csv:{campaign_id}")
        self.retry(exc=e)
    finally:
        db.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=120)
def research_user_company_worker(self, campaign_id: str):
    """
    Track B: Brand Intelligence Agent (User Intel)
    """
    db = SessionLocal()
    try:
        from app.services.user_intel_service import UserIntelService
        from app.core.security import acquire_lock, release_lock
        from app.services.campaign_service import campaign_service
        
        lock_key = f"campaign_intel:{campaign_id}"
        if not acquire_lock(lock_key, ttl=900):
            logger.warning(f"⚠️  [Track B] Research already in progress for {campaign_id}. Skipping.")
            return

        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            release_lock(lock_key)
            return

        intel = campaign.user_intel
        if not intel:
            logger.error(f"❌ [Track B] No UserIntel record for {campaign_id}")
            release_lock(lock_key)
            return

        logger.info(f"🧠 [Track B] Building Brand Brain for: {intel.website}")
        user_intel_svc = UserIntelService()
        
        # Parallel-optimized research fetch
        research_data = asyncio.run(user_intel_svc.research_user_company(intel.website, campaign.prompt))
        
        if research_data:
            intel.company_name = research_data.get("exact_company_name")
            intel.website = research_data.get("website")
            intel.motto = research_data.get("motto")
            intel.offerings = json.dumps(research_data.get("core_offerings"))
            intel.deep_research = research_data.get("deep_research")
            
            # High-Fidelity Dossier Persistence
            intel.target_customers = research_data.get("target_customers")
            intel.competitive_advantages = research_data.get("competitive_advantages")
            intel.proof_points = research_data.get("proof_points")
            intel.capability_to_pain_map = research_data.get("capability_to_pain_map")
            
            intel.v2_intel = research_data
            
            # Transition to Stage 2 Complete (ONLY if not already further along)
            if campaign.status in [models.CampaignStatus.PENDING, models.CampaignStatus.STAGE_1_CSV_TRIMMED]:
                campaign.status = models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE
            
            db.commit()
            
            logger.info(f"✅ [Track B] Brand Intelligence Secured for {campaign_id}. Advancing State Machine.")
            
            # Re-trigger State Machine to move to Stage 3
            from app.services.campaign_service import campaign_service
            asyncio.run(campaign_service.process_state_machine(db, campaign_id))
            
            release_lock(lock_key)
    except Exception as e:
        logger.error(f"Track B Failure: {e}")
        release_lock(f"campaign_intel:{campaign_id}")
        self.retry(exc=e)
    finally:
        db.close()
    


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def validate_input_worker(self, campaign_id: str):
    """
    Track C: Input Validation Agent (Agent B)
    Validates the campaign prompt for clarity and actionability.
    """
    db = SessionLocal()
    try:
        from app.services.campaign_service import campaign_service
        from app.core.security import acquire_lock, release_lock
        
        lock_key = f"campaign_val:{campaign_id}"
        if not acquire_lock(lock_key, ttl=600):
            logger.warning(f"⚠️  [Track C] Validation already in progress for {campaign_id}. Skipping.")
            return

        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            release_lock(lock_key)
            return

        logger.info(f"🔍 [Track C] Validating Input for Campaign: {campaign_id}")
        
        # Execute Validation
        validation_data = asyncio.run(CampaignValidator.validate_prompt(campaign.prompt))
        
        if validation_data:
            campaign.input_validation_review = validation_data
            # We don't change status to STAGE_2 yet because that's for User Intel.
            # We check for all tracks completion in the state machine.
            db.commit()
            
            logger.info(f"✅ [Track C] Input Validation Complete for {campaign_id}. Advancing State Machine.")
            
            # Re-trigger State Machine
            asyncio.run(campaign_service.process_state_machine(db, campaign_id))
            
            release_lock(lock_key)
    except Exception as e:
        logger.error(f"Track C Failure: {e}")
        release_lock(f"campaign_val:{campaign_id}")
        self.retry(exc=e)
    finally:
        db.close()
