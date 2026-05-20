from app.db import models
from app.workers.config.celery_app import celery_app
from app.workers.utils import acquire_lease, release_lease, db_session
from app.core.logging_config import logger
import datetime
from datetime import UTC

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_companies_worker(self, campaign_id: str):
    """
    STAGE 3: ICP Filtering (AI Gatekeeper)
    """
    from app.services.campaign_service import campaign_service
    from app.core.security import acquire_lock, release_lock
    
    lock_key = f"campaign_stage3:{campaign_id}"
    if not acquire_lock(lock_key, ttl=1200): return

    # 1. Short read transaction to load CSV contents
    with db_session() as db:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            release_lock(lock_key)
            return
        content = campaign.trimmed_csv_data
        csv_file_url = campaign.csv_file_url

    if not content and csv_file_url:
        import os
        if os.path.exists(csv_file_url):
            with open(csv_file_url, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
    
    if content:
        # Load unique companies in a short read session
        with db_session() as db:
            from app.services.csv_service import CSVProcessingService
            csv_svc = CSVProcessingService()
            _, unique_cos = csv_svc.process_csv_content(content.encode('utf-8'), None, None, None, campaign_id, db)
        
        # 2. Stage 3 logic runs with ZERO database sessions held
        import asyncio
        asyncio.run(campaign_service.stage_3_icp_filtering(None, campaign_id, unique_cos))
        
        # 3. Short write transaction to update campaign status and trigger state machine
        with db_session() as db:
            campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = models.CampaignStatus.STAGE_3_ICP_FILTERED
                db.commit()
            
            logger.info(f"✅ [STAGE 3] ICP Filtering Complete for {campaign_id}. Triggering State Machine.")
            campaign_service.process_state_machine(db, campaign_id)
    
    release_lock(lock_key)

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def deep_research_worker(self, campaign_id: str):
    """
    STAGE 4: Deep Research Swarm
    """
    from app.services.campaign_service import campaign_service
    from app.core.security import acquire_lock, release_lock
    
    lock_key = f"campaign_stage4:{campaign_id}"
    if not acquire_lock(lock_key, ttl=3600): return

    # 1. Run deep research swarm holding ZERO database connections in Celery worker
    import asyncio
    try:
        asyncio.run(campaign_service.stage_4_deep_research(None, campaign_id))
    except Exception as e:
        logger.error(f"[STAGE 4] Swarm Failure for {campaign_id}: {e}", exc_info=True)
        release_lock(lock_key)
        self.retry(exc=e)
        return

    # 2. Short write transaction to finalize Stage 4 and pulse State Machine
    with db_session() as db:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if campaign:
            campaign.status = models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE
            db.commit()

        logger.info(f"✅ [STAGE 4] Deep Research Complete for {campaign_id}. Triggering State Machine.")
        campaign_service.process_state_machine(db, campaign_id)
        
    release_lock(lock_key)

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_dms_worker(self, campaign_id: str):
    """
    STAGE 5: Strategic Stakeholder Ranking
    """
    from app.services.campaign_service import campaign_service
    from app.core.security import acquire_lock, release_lock
    
    lock_key = f"campaign_stage5:{campaign_id}"
    if not acquire_lock(lock_key, ttl=1200): return

    # 1. Short read transaction to load CSV data
    with db_session() as db:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            release_lock(lock_key)
            return
        content = campaign.trimmed_csv_data
        csv_file_url = campaign.csv_file_url

    if not content and csv_file_url:
        import os
        if os.path.exists(csv_file_url):
            with open(csv_file_url, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
    
    if content:
        # Load contacts map in a short read session
        with db_session() as db:
            from app.services.csv_service import CSVProcessingService
            csv_svc = CSVProcessingService()
            contacts_map, _ = csv_svc.process_csv_content(content.encode('utf-8'), None, None, None, campaign_id, db)
        
        # 2. Stage 5 logic runs with ZERO database sessions held
        import asyncio
        asyncio.run(campaign_service.stage_5_stakeholder_ranking(None, campaign_id, contacts_map))
        
        # 3. Short write transaction to update status and trigger state machine
        with db_session() as db:
            campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED
                db.commit()

            logger.info(f"✅ [STAGE 5] Stakeholder Ranking Complete for {campaign_id}. Triggering State Machine.")
            campaign_service.process_state_machine(db, campaign_id)
    
    release_lock(lock_key)
