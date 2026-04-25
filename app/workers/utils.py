import datetime
from datetime import UTC
from sqlalchemy.orm import Session
from app.db import models
from app.core.logging_config import logger

def acquire_lease(db: Session, campaign_id: str, worker_id: str) -> bool:
    """
    Acquires a distributed lease for a specific campaign.
    Prevents concurrent execution and sets the initial heartbeat.
    """
    now = datetime.datetime.now(UTC)
    threshold = now - datetime.timedelta(minutes=10)
    
    campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
    if not campaign:
        return False
        
    if campaign.locked_by and campaign.last_heartbeat and campaign.last_heartbeat > threshold:
        if campaign.locked_by != worker_id:
            logger.warning(f"[LEASE BLOCKED] Campaign {campaign_id} currently locked by {campaign.locked_by}. Worker {worker_id} aborting.")
            return False
            
    campaign.locked_by = worker_id
    campaign.last_heartbeat = now
    db.commit()
    return True

def heartbeat_lease(db: Session, campaign_id: str, worker_id: str):
    """
    Updates the heartbeat for an active lease to prevent the sweeper from reclaiming it.
    """
    try:
        db.query(models.Campaign).filter(
            models.Campaign.id == campaign_id,
            models.Campaign.locked_by == worker_id
        ).update({"last_heartbeat": datetime.datetime.now(UTC)}, synchronize_session=False)
        db.commit()
    except Exception as e:
        logger.error(f"[HEARTBEAT FAILURE] Failed to pulse for campaign {campaign_id}: {e}")

def release_lease(db: Session, campaign_id: str, worker_id: str):
    """
    Releases a campaign lease upon successful phase completion or terminal failure.
    """
    try:
        db.query(models.Campaign).filter(
            models.Campaign.id == campaign_id,
            models.Campaign.locked_by == worker_id
        ).update({"locked_by": None, "last_heartbeat": datetime.datetime.now(UTC)}, synchronize_session=False)
        db.commit()
    except Exception as e:
        logger.error(f"[LEASE RELEASE FAILURE] Failed to release lock for campaign {campaign_id}: {e}")
