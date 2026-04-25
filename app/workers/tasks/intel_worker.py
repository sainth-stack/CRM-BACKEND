"""
Phase 1: User Company Deep Research Worker
Celery task that researches the user's own company and triggers Phase 2.
"""
from app.db.database import SessionLocal
from app.db import models
from app.agents.user_intel import research_user_company
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
import json
import datetime
from datetime import UTC


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def research_user_company_worker(self, campaign_id: str):
    """
    Phase 1: Deep user-company capability extraction.
    Researches the user's base domain to build a capability model for AI-driven emails.
    """
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.warning(f"Campaign {campaign_id} not found in intel worker.")
            return

        # Temporal Boundary Check: Pause worker for expired trials
        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                logger.info(f"[MISSION CONTROL] Suspension: Campaign {campaign_id} owner trial expired.")
                return

        intel = campaign.user_intel
        if not intel:
            logger.warning(f"No user intel profile for campaign {campaign_id}.")
            return

        from app.workers.utils import acquire_lease, release_lease
        worker_id = f"worker:{self.request.id}"
        if not acquire_lease(db, campaign_id, worker_id):
            return

        logger.info(f"Starting deep research for campaign {campaign_id} on {intel.website}")
        research_data = research_user_company(intel.website)
        if research_data:
            intel.company_name = research_data.get("exact_company_name")
            intel.website = research_data.get("website")
            intel.motto = research_data.get("moto")
            intel.offerings = json.dumps(research_data.get("core_offerings"))
            intel.deep_research = research_data.get("deep_research")
            db.commit()
            
            # Explicit Lease Release: Handoff to next cluster
            release_lease(db, campaign_id, worker_id)
            check_phase_1_completion(campaign_id)
    except Exception as e:
        logger.error(f"User Research Critical Error for campaign {campaign_id}: {e}", exc_info=True)
        # Clear lease on failure to allow retry/recovery
        try: release_lease(db, campaign_id, f"worker:{self.request.id}")
        except: pass
        self.retry(exc=e)
    finally:
        db.close()


def check_phase_1_completion(campaign_id: str):
    """
    Synchronization Gate: Triggers Phase 2 only when deep research is validated.
    Ensures that target company discovery only begins after high-fidelity intel is secured.
    """
    from app.workers.tasks.discovery_worker import find_companies_worker
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        intel = campaign.user_intel

        # Criteria: Deep Research Validation Gate & Status Guard
        if (
            campaign.status == models.CampaignStatus.RESEARCHING_USER_COMPANY and
            intel and intel.deep_research and intel.deep_research not in [
                "Analysis pending deep synchronization.",
                "Identity verified through site architecture."
            ]
        ):
            logger.info(f"[MISSION CONTROL] User Intel Phase Complete for {campaign_id}. Dispatching Discovery Cluster.")
            campaign.status = models.CampaignStatus.FINDING_TARGET_COMPANIES
            db.commit()
            find_companies_worker.delay(campaign_id)
        else:
            logger.info(f"[MISSION CONTROL] Intel validation failed for {campaign_id}. Halting progression.")
    except Exception as e:
        db.rollback()
        logger.error(f"Synchronization Gate Error for campaign {campaign_id}: {e}")
    finally:
        db.close()
