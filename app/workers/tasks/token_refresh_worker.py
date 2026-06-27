"""
Proactive Token Refresh Worker.
Periodically refreshes OAuth tokens before expiration to ensure seamless 
Gmail and Calendar integrations without manual user intervention.
"""

from app.workers.config.celery_app import celery_app
from app.db.database import SessionLocal
from app.db import models
from app.core.token_service import TokenService
from app.core.logging_config import logger


@celery_app.task(bind=True, max_retries=2)
def refresh_expiring_tokens_task(self):
    """
    Background: Refresh Google and Cal.com tokens approaching expiration.
    Runs every 6 hours (configurable via Celery Beat).
    """
    db = SessionLocal()
    
    try:
        logger.info("[TOKEN-REFRESH] Starting proactive token refresh cycle")
        
        # 1. Refresh Google OAuth tokens expiring within 24 hours
        google_accounts = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.provider == "google",
            models.OAuthAccount.encrypted_refresh_token.isnot(None)
        ).all()
        
        google_refreshed = 0
        google_failed = 0
        
        for oauth_acc in google_accounts:
            try:
                success = TokenService.refresh_and_update_access(
                    db, 
                    oauth_acc.user_id,
                    provider="google"
                )
                if success:
                    google_refreshed += 1
                else:
                    google_failed += 1
            except Exception as e:
                logger.error(f"[TOKEN-REFRESH] Failed to refresh Google token for user {oauth_acc.user_id}: {e}")
                google_failed += 1
        
        # 2. Verify Cal.com tokens (typically long-lived)
        cal_users = db.query(models.User).filter(
            models.User.cal_access_token.isnot(None)
        ).all()
        
        cal_verified = 0
        cal_failed = 0
        
        for user in cal_users:
            try:
                success = TokenService.refresh_and_update_access(
                    db,
                    user.id,
                    provider="cal.com"
                )
                if success:
                    cal_verified += 1
                else:
                    cal_failed += 1
            except Exception as e:
                logger.error(f"[TOKEN-REFRESH] Failed to verify Cal.com token for user {user.id}: {e}")
                cal_failed += 1
        
        result = {
            "google_refreshed": google_refreshed,
            "google_failed": google_failed,
            "cal_verified": cal_verified,
            "cal_failed": cal_failed
        }
        
        logger.info(f"[TOKEN-REFRESH] Token refresh cycle complete: {result}")
        return result
        
    except Exception as e:
        logger.critical(f"[TOKEN-REFRESH] Critical error in token refresh worker: {e}")
        # Retry task after 5 minutes
        raise self.retry(exc=e, countdown=300)
    finally:
        db.close()


@celery_app.task
def schedule_token_refresh():
    """
    Quick endpoint to manually trigger token refresh.
    Useful for testing or manual intervention.
    """
    result = refresh_expiring_tokens_task.delay()
    logger.info(f"[TOKEN-REFRESH] Scheduled manual token refresh. Task ID: {result.id}")
    return {"task_id": str(result.id), "status": "scheduled"}
