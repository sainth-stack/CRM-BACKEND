from app.db.database import SessionLocal
from app.db import models
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
from app.services.campaign_service import campaign_service
from app.workers.utils import acquire_lease, release_lease
import datetime
from datetime import UTC

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_companies_worker(self, campaign_id: str):
    """
    Worker for identifying companies matching the campaign's ICP.
    Delegates research logic to CampaignService.
    """
    db = SessionLocal()
    worker_id = f"worker:{self.request.id}"
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            return

        # Demo Guard
        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                logger.info(f"[DISCOVERY] Trial expired for campaign {campaign_id}.")
                return

        if not acquire_lease(db, campaign_id, worker_id):
            return

        # Delegate to Service
        batch_count = campaign_service.run_company_discovery(db, campaign_id, worker_id)

        # Transition
        if batch_count == 0 and db.query(models.TargetCompany).filter(models.TargetCompany.campaign_id == campaign_id).count() == 0:
            campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
            campaign.status_reason = "No target companies found matching criteria."
            db.commit()
            logger.warning(f"[DISCOVERY] Campaign {campaign_id} stalled: No companies found.")
        else:
            campaign.status = models.CampaignStatus.FINDING_DECISION_MAKERS
            db.commit()
            find_dms_worker.delay(campaign_id)

        release_lease(db, campaign_id, worker_id)
    except Exception as e:
        logger.error(f"[DISCOVERY] Failure for {campaign_id}: {e}", exc_info=True)
        try: release_lease(db, campaign_id, worker_id)
        except: pass
        self.retry(exc=e)
    finally:
        db.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_dms_worker(self, campaign_id: str):
    """
    Worker for identifying decision-makers for discovered companies.
    Delegates research logic to CampaignService.
    """
    db = SessionLocal()
    worker_id = f"worker:{self.request.id}"
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign or not acquire_lease(db, campaign_id, worker_id):
            return

        # Delegate to Service
        campaign_service.identify_stakeholders(db, campaign_id, worker_id)

        # Final Status Update
        campaign.status = models.CampaignStatus.DRAFTING_EMAILS
        db.commit()
        
        from app.workers.tasks.ghostwriter_worker import draft_emails_worker
        draft_emails_worker.delay(campaign_id)

        release_lease(db, campaign_id, worker_id)
    except Exception as e:
        logger.error(f"[DM FINDER] Failure for {campaign_id}: {e}")
        try: release_lease(db, campaign_id, worker_id)
        except: pass
        self.retry(exc=e)
    finally:
        db.close()
