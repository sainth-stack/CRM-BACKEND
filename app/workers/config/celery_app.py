from celery import Celery, Task
import os
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

load_dotenv()

from app.core.config import settings

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
        
        # Heuristic: campaign_id extraction
        # If bound task, first arg in *args is usually 'self'
        cid = kwargs.get("campaign_id")
        if not cid and args:
            # If the first arg is the task instance itself, skip it
            potential_cid = args[1] if (len(args) > 1 and args[0] == self) else args[0]
            if isinstance(potential_cid, str): cid = potential_cid
        
        token = campaign_id_var.set(cid)
        try:
            # We call run() directly to avoid super().__call__ duplication issues 
            # with bound methods in complex inheritance chains.
            return self.run(*args, **kwargs)
        finally:
            campaign_id_var.reset(token)
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """
        Terminal Failure Handler: Ensures campaigns don't stay in 'PENDING' forever
        if a worker crashes or retries are exhausted.
        """
        from app.workers.utils import db_session
        from app.db import models

        campaign_id = args[0] if args else kwargs.get("campaign_id")
        if not campaign_id:
            return

        with db_session() as db:
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
                    logger.error(f"[TERMINAL FAILURE] Campaign {campaign_id} marked FAILED "
                                 f"after exhausting all retries. Root cause: {exc}")
            except Exception as db_err:
                logger.critical(f"[TERMINAL FAILURE] Could not update campaign status: {db_err}")


from celery.signals import task_failure

@task_failure.connect
def handle_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **extra):
    """Fire on any Celery task failure and send a structured alert to the logs
    (and to Slack if SLACK_WEBHOOK_URL is configured)."""
    task_name = sender.name if sender else "UnknownTask"
    error_msg = (
        f"🚨 *[CELERY FAILURE]* Task `{task_name}` (ID: `{task_id}`) failed in production!\n"
        f"*Args*: `{args}`\n"
        f"*Kwargs*: `{kwargs}`\n"
        f"*Exception*: `{exception}`"
    )
    logger.critical(error_msg, exc_info=True)
    
    slack_webhook = os.getenv("SLACK_WEBHOOK_URL")
    if slack_webhook:
        try:
            import httpx
            payload = {
                "attachments": [
                    {
                        "title": "🚨 Background task failure",
                        "text": error_msg,
                        "color": "#FF0000",
                        "mrkdwn_in": ["text"]
                    }
                ]
            }
            httpx.post(slack_webhook, json=payload, timeout=5.0)
        except Exception as slack_err:
            logger.error(f"Failed to dispatch Slack webhook notification for Celery error: {slack_err}")


celery_app = Celery(
    "outreach_tasks",
    broker=redis_url,
    backend=redis_url,
    include=[
        "app.workers.tasks.intel_worker",
        "app.workers.tasks.discovery_worker",
        "app.workers.tasks.ghostwriter_worker",
        "app.workers.tasks.outbound_worker",
        "app.workers.tasks.inbox_worker",
        "app.workers.tasks.orchestrator_worker",
        "app.workers.tasks.reminders_worker",
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
    task_track_started=False,
    task_ignore_result=True,      # Do not store results in Redis to save commands
    task_time_limit=3600,
    task_acks_late=True,          # Acknowledge AFTER task completes (not before)
    task_reject_on_worker_lost=True,  # Re-queue if worker crashes mid-execution
    broker_connection_retry_on_startup=True,

    # --- Idle Redis-command reduction (Upstash bills per command) ---
    # Nothing in the app uses Celery remote control, inspect, task events, or Flower,
    # so turn off the per-worker pidbox control mailbox + event emission. This removes
    # the constant idle polling/publishing of those side-channels with ZERO effect on
    # task execution (speed/throughput/latency are unchanged — only the admin/monitoring
    # channels are dropped, which we don't use).
    worker_enable_remote_control=False,
    worker_send_task_events=False,
    task_send_sent_event=False,

    # --- Memory guardrails (OOM defense in depth for heavy_research) ---
    # Heavy research tasks (Stage 3/4 website-crawl + LLM swarms) are the main RAM
    # drivers. These recycle the worker process *between tasks* before the box
    # OOM-kills it mid-commit, so a clean restart picks up the next chunk/task.
    worker_prefetch_multiplier=1,     # don't hoard tasks on a heavy worker
    worker_max_tasks_per_child=20,    # recycle process periodically (defeats fragmentation/leaks)
    # KB. Restart the child once RSS crosses this, between tasks. Tuned for a
    # ~512MB instance — leaves headroom for the broker/parent. Override via env.
    worker_max_memory_per_child=int(os.getenv("WORKER_MAX_MEMORY_KB", "380000")),

    # --- Upstash (managed Redis) connection & cost governance ---
    # Upstash caps concurrent connections AND bills per command, so keep the
    # broker connection pool small and disable result storage entirely.
    broker_pool_limit=3,              # default 10 -> too many conns across processes
    redis_max_connections=20,         # hard ceiling on the redis client pool
    result_backend=None,              # we already ignore results; don't open a backend
    result_expires=600,
    broker_transport_options={
        "visibility_timeout": 3700,   # must exceed task_time_limit so tasks aren't redelivered mid-run
        "socket_keepalive": True,
    },
)

# --- SSL Security Overlay for Secure Redis (Upstash/rediss) ---
if redis_url.startswith("rediss"):
    import ssl
    celery_app.conf.update(
        broker_use_ssl={"ssl_cert_reqs": ssl.CERT_REQUIRED},
        redis_backend_use_ssl={"ssl_cert_reqs": ssl.CERT_REQUIRED}
    )

# --- Task Routing Architecture ---
# Split queues by workload to guarantee zero delay for critical flows.
celery_app.conf.task_routes = {
    # 1. Heavy AI and Deep Research Workloads (extremely slow, heavy CPU/RAM)
    "app.workers.tasks.intel_worker.*": {"queue": "heavy_research"},
    "app.workers.tasks.discovery_worker.*": {"queue": "heavy_research"},
    "app.workers.tasks.ghostwriter_worker.*": {"queue": "heavy_research"},

    # 2. Critical Outbound Email Dispatches (low latency, high priority)
    "app.workers.tasks.outbound_worker.*": {"queue": "outbound_dispatch"},

    # 3. High-Frequency Inbox Polling (independent email sweeps)
    "app.workers.tasks.inbox_worker.*": {"queue": "inbox_polling"},

    # 4. Orchestration & Scheduler Sweepers (infrastructure and lifecycle tasks)
    "app.workers.tasks.orchestrator_worker.*": {"queue": "orchestrator"},
    "app.workers.tasks.reminders_worker.*": {"queue": "orchestrator"},
    "app.workers.tasks.sweeper_worker.*": {"queue": "orchestrator"},
}

celery_app.conf.beat_schedule = {
    "poll-inboxes": {
        "task": "app.workers.tasks.inbox_worker.poll_all_users_task",
        "schedule": float(settings.INBOX_POLL_SECONDS),
    },
    "check-meetings": {
        "task": "app.workers.tasks.reminders_worker.check_upcoming_meetings_task",
        "schedule": float(settings.MEETING_CHECK_SECONDS),
    },
    "check-inactivity": {
        "task": "app.workers.tasks.orchestrator_worker.check_all_inactivity_task",
        "schedule": float(settings.INACTIVITY_CHECK_SECONDS),
    },
    "reactivate-terminated": {
        "task": "app.workers.tasks.orchestrator_worker.reactivate_terminated_prospects_task",
        "schedule": float(settings.REACTIVATION_CHECK_SECONDS),
    },
    "sweep-stuck-campaigns": {
        "task": "app.workers.tasks.sweeper_worker.sweep_stuck_campaigns_task",
        "schedule": float(settings.SWEEP_STUCK_CAMPAIGNS_SECONDS),
    },
    "sweep-stranded-dispatches": {
        "task": "app.workers.tasks.sweeper_worker.sweep_stranded_dispatches_task",
        "schedule": float(settings.SWEEP_STRANDED_DISPATCHES_SECONDS),
    },
    "dispatch-due-drafts": {
        "task": "app.workers.tasks.outbound_worker.dispatch_due_drafts_task",
        "schedule": float(settings.DISPATCH_POLL_SECONDS),
        "options": {"queue": "outbound_dispatch"},
    },
    # Proactive OAuth token refresh sweep removed: both Google (TokenService.
    # get_google_credentials) and Cal.com (cal_provider.get_valid_access_token)
    # already lazy-refresh on demand at every call site - settings-page loads,
    # bookings, slot lookups, drafting - so a periodic global sweep across every
    # user was redundant idle Redis/Celery traffic on top of the on-demand path.
}
