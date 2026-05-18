from app.db.database import SessionLocal
from app.db import models
from app.core.logging_config import logger
from app.workers.config.celery_app import celery_app
import datetime
from datetime import UTC
from app.core.config import settings


@celery_app.task
def sweep_stuck_campaigns_task():
    """
    Resurrection Protocol: Recovers ephemeral operations lost to server restarts or unexpected infrastructure failures.
    Scans the database for campaigns in active but non-terminal states and re-injects them into the appropriate Celery task queue.
    """
    logger.info("[SENTINEL] Sweeping for ghosted background operations...")
    db = SessionLocal()
    try:
        # Recovery Gate: Only resurrect operations that have lost their heartbeat lease.
        # Threshold: 10 minutes of silence indicates a crashed worker.
        threshold = datetime.datetime.now(UTC) - datetime.timedelta(minutes=settings.SWEEP_STUCK_MINUTES)
        
        stuck_campaigns = db.query(models.Campaign).filter(
            models.Campaign.status.in_([
                models.CampaignStatus.PENDING,
                models.CampaignStatus.INPUT_VALIDATED,
                models.CampaignStatus.RESEARCHING_USER_COMPANY,
                models.CampaignStatus.STAGE_1_CSV_TRIMMED,
                models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE,
                models.CampaignStatus.STAGE_3_ICP_FILTERED,
                models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE,
                models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED,
                models.CampaignStatus.STAGE_6_DRAFTING_COMPLETE,
                "INTERVENTION_NEEDED"
            ]),
            (models.Campaign.last_heartbeat == None) | (models.Campaign.last_heartbeat < threshold)
        ).all()

        count = len(stuck_campaigns)
        if count == 0:
            logger.debug("[SENTINEL] No ghosted operations found or all active leases are current.")
            return

        logger.info(f"[SENTINEL] Discovered {count} dropped operations with expired leases. Initializing V3 Resurrection...")

        from app.workers.tasks.intel_worker import process_csv_worker

        for campaign in stuck_campaigns:
            owner = campaign.owner
            if owner and owner.is_demo and owner.demo_expires_at:
                if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                    logger.info(f"[RECOVERY] Skipping Campaign {campaign.id} due to expired trial.")
                    continue

            logger.info(f"[RECOVERY] Resurrecting Campaign {campaign.id} from Stage: {campaign.status.name}")
            
            campaign.last_heartbeat = None
            campaign.locked_by = None
            db.commit()

            from app.services.campaign_service import campaign_service
            import asyncio
            asyncio.run(campaign_service.process_state_machine(db, campaign.id))

    except Exception as e:
        logger.error(f"[SENTINEL] Critical error during Resurrection Protocol sweep: {e}", exc_info=True)
    finally:
        db.close()
