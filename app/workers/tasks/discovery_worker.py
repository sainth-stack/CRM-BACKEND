from app.db.database import SessionLocal
from app.db import models
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
from app.workers.utils import acquire_lease, release_lease
import datetime
from datetime import UTC

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_companies_worker(self, campaign_id: str):
    """
    STAGE 3: ICP Filtering (AI Gatekeeper)
    """
    db = SessionLocal()
    try:
        from app.services.campaign_service import campaign_service
        from app.core.security import acquire_lock, release_lock
        
        lock_key = f"campaign_stage3:{campaign_id}"
        if not acquire_lock(lock_key, ttl=1200): return

        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            release_lock(lock_key)
            return

        # Load CSV content
        content = campaign.trimmed_csv_data
        if not content and campaign.csv_file_url:
            import os
            if os.path.exists(campaign.csv_file_url):
                with open(campaign.csv_file_url, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
        
        if content:
            from app.services.csv_service import CSVProcessingService
            csv_svc = CSVProcessingService()
            _, unique_cos = csv_svc.process_csv_content(content.encode('utf-8'), None, None, None, campaign_id, db)
            
            # This is Stage 3 Logic
            import asyncio
            asyncio.run(campaign_service.stage_3_icp_filtering(db, campaign_id, unique_cos))
            
            campaign.status = models.CampaignStatus.STAGE_3_ICP_FILTERED
            db.commit()
            
            logger.info(f"✅ [STAGE 3] ICP Filtering Complete for {campaign_id}. Triggering State Machine.")
            asyncio.run(campaign_service.process_state_machine(db, campaign_id))
        
        release_lock(lock_key)
    except Exception as e:
        logger.error(f"[STAGE 3] Failure for {campaign_id}: {e}", exc_info=True)
        release_lock(f"campaign_stage3:{campaign_id}")
        self.retry(exc=e)
    finally:
        db.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def deep_research_worker(self, campaign_id: str):
    """
    STAGE 4: Deep Research Swarm
    """
    db = SessionLocal()
    try:
        from app.services.campaign_service import campaign_service
        from app.core.security import acquire_lock, release_lock
        
        lock_key = f"campaign_stage4:{campaign_id}"
        if not acquire_lock(lock_key, ttl=3600): return

        import asyncio
        asyncio.run(campaign_service.stage_4_deep_research(db, campaign_id))
        
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        campaign.status = models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE
        db.commit()

        logger.info(f"✅ [STAGE 4] Deep Research Complete for {campaign_id}. Triggering State Machine.")
        asyncio.run(campaign_service.process_state_machine(db, campaign_id))
        
        release_lock(lock_key)
    except Exception as e:
        logger.error(f"[STAGE 4] Failure for {campaign_id}: {e}", exc_info=True)
        release_lock(f"campaign_stage4:{campaign_id}")
        self.retry(exc=e)
    finally:
        db.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_dms_worker(self, campaign_id: str):
    """
    STAGE 5: Strategic Stakeholder Ranking
    """
    db = SessionLocal()
    try:
        from app.services.campaign_service import campaign_service
        from app.core.security import acquire_lock, release_lock
        
        lock_key = f"campaign_stage5:{campaign_id}"
        if not acquire_lock(lock_key, ttl=1200): return

        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            release_lock(lock_key)
            return

        # Load contacts_map for G
        content = campaign.trimmed_csv_data
        if not content and campaign.csv_file_url:
            import os
            if os.path.exists(campaign.csv_file_url):
                with open(campaign.csv_file_url, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
        
        if content:
            from app.services.csv_service import CSVProcessingService
            csv_svc = CSVProcessingService()
            contacts_map, _ = csv_svc.process_csv_content(content.encode('utf-8'), None, None, None, campaign_id, db)
            
            import asyncio
            asyncio.run(campaign_service.stage_5_stakeholder_ranking(db, campaign_id, contacts_map))
            
            campaign.status = models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED
            db.commit()

            logger.info(f"✅ [STAGE 5] Stakeholder Ranking Complete for {campaign_id}. Triggering State Machine.")
            asyncio.run(campaign_service.process_state_machine(db, campaign_id))
        
        release_lock(lock_key)
    except Exception as e:
        logger.error(f"[STAGE 5] Failure for {campaign_id}: {e}", exc_info=True)
        release_lock(f"campaign_stage5:{campaign_id}")
        self.retry(exc=e)
    finally:
        db.close()
