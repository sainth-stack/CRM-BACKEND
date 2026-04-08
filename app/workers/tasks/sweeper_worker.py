"""
Resurrection Protocol: Sweeper Worker
Scans for campaigns interrupted by server crashes and re-injects them into the Celery queue.
"""
from app.db.database import SessionLocal
from app.db import models
import datetime
from datetime import UTC


def sweep_stuck_campaigns_task():
    """Resurrection Protocol: Recovers ephemeral operations lost to server restarts."""
    print("[SENTINEL] Sweeping for ghosted background operations...")
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
            print("[SENTINEL] No ghosted operations found. Memory state is pristine.")
            return

        print(f"[SENTINEL] Discovered {count} dropped operations. Initializing resurrection sequence...")

        # Import here to avoid circular imports
        from app.workers.tasks.intel_worker import research_user_company_worker
        from app.workers.tasks.discovery_worker import find_companies_worker, find_dms_worker
        from app.workers.tasks.ghostwriter_worker import draft_emails_worker

        for campaign in stuck_campaigns:
            owner = campaign.owner
            if owner and owner.is_demo and owner.demo_expires_at:
                if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                    continue

            print(f"[RECOVERY] Resurrecting Campaign {campaign.id} at stage: {campaign.status.name}")

            if campaign.status == models.CampaignStatus.RESEARCHING_USER_COMPANY:
                research_user_company_worker.delay(campaign.id)
            elif campaign.status == models.CampaignStatus.FINDING_TARGET_COMPANIES:
                find_companies_worker.delay(campaign.id)
            elif campaign.status == models.CampaignStatus.FINDING_DECISION_MAKERS:
                find_dms_worker.delay(campaign.id)
            elif campaign.status == models.CampaignStatus.DRAFTING_EMAILS:
                draft_emails_worker.delay(campaign.id)

    except Exception as e:
        print(f"[SENTINEL] Deep Sweeper Failure: {e}")
    finally:
        db.close()
