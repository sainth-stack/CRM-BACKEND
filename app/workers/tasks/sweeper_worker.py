from app.db.database import SessionLocal
from app.db import models
from app.core.logging_config import logger
import datetime
from datetime import UTC


def sweep_stuck_campaigns_task():
    """
    Resurrection Protocol: Recovers ephemeral operations lost to server restarts or unexpected infrastructure failures.
    Scans the database for campaigns in active but non-terminal states and re-injects them into the appropriate Celery task queue.
    """
    logger.info("[SENTINEL] Sweeping for ghosted background operations...")
    db = SessionLocal()
    try:
        stuck_campaigns = db.query(models.Campaign).filter(
            models.Campaign.status.in_([
                models.CampaignStatus.RESEARCHING_USER_COMPANY,
                models.CampaignStatus.FINDING_TARGET_COMPANIES,
                models.CampaignStatus.FINDING_DECISION_MAKERS,
                models.CampaignStatus.DRAFTING_EMAILS
            ])
        ).all()

        count = len(stuck_campaigns)
        if count == 0:
            logger.debug("[SENTINEL] No ghosted operations found. System state is pristine.")
            return

        logger.info(f"[SENTINEL] Discovered {count} dropped operations. Initializing resurrection sequence...")

        # Late import to prevent circular dependency cycles
        from app.workers.tasks.intel_worker import research_user_company_worker
        from app.workers.tasks.discovery_worker import find_companies_worker, find_dms_worker
        from app.workers.tasks.ghostwriter_worker import draft_emails_worker

        for campaign in stuck_campaigns:
            owner = campaign.owner
            if owner and owner.is_demo and owner.demo_expires_at:
                if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                    logger.info(f"[RECOVERY] Skipping Campaign {campaign.id} due to expired trial.")
                    continue

            logger.info(f"[RECOVERY] Resurrecting Campaign {campaign.id} from coordinate: {campaign.status.name}")

            if campaign.status == models.CampaignStatus.RESEARCHING_USER_COMPANY:
                research_user_company_worker.delay(campaign.id)
            elif campaign.status == models.CampaignStatus.FINDING_TARGET_COMPANIES:
                find_companies_worker.delay(campaign.id)
            elif campaign.status == models.CampaignStatus.FINDING_DECISION_MAKERS:
                find_dms_worker.delay(campaign.id)
            elif campaign.status == models.CampaignStatus.DRAFTING_EMAILS:
                draft_emails_worker.delay(campaign.id)

    except Exception as e:
        logger.error(f"[SENTINEL] Critical error during Resurrection Protocol sweep: {e}", exc_info=True)
    finally:
        db.close()
