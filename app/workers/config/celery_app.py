from celery import Celery, Task
import os
from dotenv import load_dotenv

load_dotenv()

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")


class CampaignBaseTask(Task):
    """
    Enterprise Base Task — Terminal Failure Handler.
    When all retries are exhausted, automatically marks the campaign as FAILED
    in the database. This prevents the frontend from polling indefinitely and
    gives the user a clear error state instead of an infinite loading spinner.
    """
    abstract = True
    
    def __call__(self, *args, **kwargs):
        """
        Task Lifecycle Hook: Injects the campaign_id into the thread-local context
        before execution begins. This enables the logging filter to capture and 
        tag all logs with the appropriate trace ID automatically.
        """
        from app.core.logging_config import campaign_id_var
        
        # Heuristic: campaign_id is almost always the first positional argument
        # or an explicit keyword argument in this architecture.
        campaign_id = args[0] if args else kwargs.get("campaign_id")
        
        token = campaign_id_var.set(campaign_id)
        try:
            return super().__call__(*args, **kwargs)
        finally:
            campaign_id_var.reset(token)
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """
        Terminal Failure Handler: Ensures campaigns don't stay in 'PENDING' forever
        if a worker crashes or retries are exhausted.
        """
        from app.db.database import SessionLocal
        from app.db import models

        campaign_id = args[0] if args else kwargs.get("campaign_id")
        if not campaign_id:
            return

        db = SessionLocal()
        try:
            campaign = db.query(models.Campaign).filter(
                models.Campaign.id == campaign_id
            ).first()
            if campaign and campaign.status not in [
                models.CampaignStatus.COMPLETED,
                models.CampaignStatus.FAILED
            ]:
                campaign.status = models.CampaignStatus.FAILED
                db.commit()
                print(f"[TERMINAL FAILURE] Campaign {campaign_id} marked FAILED "
                      f"after exhausting all retries. Root cause: {exc}")
        except Exception as db_err:
            print(f"[TERMINAL FAILURE] Could not update campaign status: {db_err}")
        finally:
            db.close()


celery_app = Celery(
    "outreach_tasks",
    broker=redis_url,
    backend=redis_url,
    include=[
        "app.workers.tasks.intel_worker",
        "app.workers.tasks.discovery_worker",
        "app.workers.tasks.ghostwriter_worker",
        "app.workers.tasks.outbound_worker",
        "app.workers.tasks.sentinels_worker",
        "app.workers.tasks.sweeper_worker",
    ],
    task_cls=CampaignBaseTask
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    task_acks_late=True,          # Acknowledge AFTER task completes (not before)
    task_reject_on_worker_lost=True,  # Re-queue if worker crashes mid-execution
    broker_connection_retry_on_startup=True
)

# --- SSL Security Overlay for Secure Redis (Upstash/rediss) ---
if redis_url.startswith("rediss"):
    celery_app.conf.update(
        broker_use_ssl={"ssl_cert_reqs": "none"},
        redis_backend_use_ssl={"ssl_cert_reqs": "none"}
    )

celery_app.conf.beat_schedule = {
    "poll-inboxes-every-2-minutes": {
        "task": "app.workers.tasks.sentinels_worker.poll_all_users_task",
        "schedule": 120.0,
    },
    "check-meetings-every-10-minutes": {
        "task": "app.workers.tasks.sentinels_worker.check_upcoming_meetings_task",
        "schedule": 600.0,
    },
    "check-inactivity-every-5-minutes": {
        "task": "app.workers.tasks.sentinels_worker.check_all_inactivity_task",
        "schedule": 300.0,
    },
    "reactivate-terminated-every-6-hours": {
        "task": "app.workers.tasks.sentinels_worker.reactivate_terminated_prospects_task",
        "schedule": 21600.0,
    },
    "sweep-stuck-campaigns-every-10-minutes": {
        "task": "app.workers.tasks.sweeper_worker.sweep_stuck_campaigns_task",
        "schedule": 600.0,
    },
}
