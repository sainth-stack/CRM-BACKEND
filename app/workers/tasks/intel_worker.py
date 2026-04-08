"""
Phase 1: User Company Deep Research Worker
Celery task that researches the user's own company and triggers Phase 2.
"""
from app.db.database import SessionLocal
from app.db import models
from app.agents.user_intel import research_user_company
from app.workers.config.celery_app import celery_app
import json
import datetime
from datetime import UTC


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def research_user_company_worker(self, campaign_id: str):
    """Phase 1: Deep user-company capability extraction."""
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign: return

        # Temporal Boundary Check: Pause worker for expired trials
        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                print(f"[MISSION CONTROL] Suspension: Campaign {campaign_id} owner trial expired.")
                return

        intel = campaign.user_intel
        if not intel: return

        research_data = research_user_company(intel.website)
        if research_data:
            intel.company_name = research_data.get("exact_company_name")
            intel.website = research_data.get("website")
            intel.motto = research_data.get("moto")
            intel.offerings = json.dumps(research_data.get("core_offerings"))
            intel.deep_research = research_data.get("deep_research")
            db.commit()
            check_phase_1_completion(campaign_id)
    except Exception as e:
        print(f"User Research Error: {e}")
        self.retry(exc=e)
    finally:
        db.close()


def check_phase_1_completion(campaign_id: str):
    """Synchronization Gate: Triggers Phase 2 only when deep research is validated."""
    from app.workers.tasks.discovery_worker import find_companies_worker
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        intel = campaign.user_intel

        # Criteria: Deep Research Validation Gate
        if intel and intel.deep_research and intel.deep_research not in [
            "Analysis pending deep synchronization.",
            "Identity verified through site architecture."
        ]:
            print(f"[MISSION CONTROL] User Intel Phase Complete for {campaign_id}. Dispatching Redis Task.")
            campaign.status = models.CampaignStatus.FINDING_TARGET_COMPANIES
            db.commit()
            find_companies_worker.delay(campaign_id)
        else:
            print(f"[MISSION CONTROL] Intel validation failed for {campaign_id}. Halting progression.")
    except Exception as e:
        db.rollback()
        print(f"Error in Phase 1 Gate: {e}")
    finally:
        db.close()
