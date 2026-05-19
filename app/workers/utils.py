import datetime
from datetime import UTC
from sqlalchemy.orm import Session
from app.db import models
from app.core.logging_config import logger
from typing import Callable, Any, Optional
from app.db.database import SessionLocal

def acquire_lease(db: Session, campaign_id: str, worker_id: str) -> bool:
    """
    Acquires a distributed lease for a specific campaign.
    Prevents concurrent execution and sets the initial heartbeat.
    """
    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    threshold = now - datetime.timedelta(minutes=10)
    
    campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
    if not campaign:
        return False
        
    last_hb = campaign.last_heartbeat
    if last_hb and last_hb.tzinfo is not None:
        last_hb = last_hb.replace(tzinfo=None)

    if campaign.locked_by and last_hb and last_hb > threshold:
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
        ).update({"last_heartbeat": datetime.datetime.now(UTC).replace(tzinfo=None)}, synchronize_session=False)
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
        ).update({"locked_by": None, "last_heartbeat": datetime.datetime.now(UTC).replace(tzinfo=None)}, synchronize_session=False)
        db.commit()
    except Exception as e:
        logger.error(f"[LEASE RELEASE FAILURE] Failed to release lock for campaign {campaign_id}: {e}")


def with_short_lived_db_session(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator that provides a short-lived DB session to worker functions.
    Ensures DB sessions are properly closed after use, preventing stale sessions.

    Pattern:
    i. Read from DB
    ii. Close session
    iii. Run AI/external work
    iv. Open new session and save results

    Usage:
        @with_short_lived_db_session
        def my_worker(db: Session, campaign_id: str):
            # This function gets a short-lived session
            # When it completes, session is automatically closed
    """
    def wrapper(*args, **kwargs):
        # Create a new session for this call
        db = SessionLocal()
        try:
            # Pass the session as the first argument to the function
            return func(db, *args, **kwargs)
        finally:
            # Always close the session
            db.close()
    return wrapper


def execute_with_db_session(func: Callable[[Session], Any]) -> Any:
    """
    Executes a function with a short-lived DB session.
    Used for cases where the function doesn't fit the decorator pattern.

    Usage:
        result = execute_with_db_session(lambda db: db.query(models.Campaign).first())
    """
    db = SessionLocal()
    try:
        return func(db)
    finally:
        db.close()
