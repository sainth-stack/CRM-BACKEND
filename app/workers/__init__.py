"""
app/workers/__init__.py
=======================
Master re-export hub for the workers package.
Maintains backward compatibility so main.py and the APScheduler 
do not need to know about the internal file structure.
"""

# --- Celery App (for decorator access) ---
from app.workers.config.celery_app import celery_app

# --- Phase 1: Intel ---
from app.workers.tasks.intel_worker import research_user_company_worker

# --- Phase 2: Discovery ---
from app.workers.tasks.discovery_worker import find_companies_worker, find_dms_worker

# --- Phase 3: Ghostwriter ---
from app.workers.tasks.ghostwriter_worker import (
    draft_emails_worker,
    draft_followup_worker,
    draft_discovery_worker,
)

# --- Outbound Dispatch --- 
from app.workers.tasks.outbound_worker import send_draft_worker

# --- Scheduled (cron) tasks ---
from app.workers.tasks.inbox_worker import poll_inbox_task, poll_all_users_task
from app.workers.tasks.orchestrator_worker import (
    outreach_orchestrator_worker,
    check_all_inactivity_task,
    reactivate_terminated_prospects_task,
)
from app.workers.tasks.reminders_worker import check_upcoming_meetings_task

# --- Sweeper (Startup resurrection) ---
from app.workers.tasks.sweeper_worker import sweep_stuck_campaigns_task

__all__ = [
    "celery_app",
    "research_user_company_worker",
    "find_companies_worker",
    "find_dms_worker",
    "draft_emails_worker",
    "draft_followup_worker",
    "draft_discovery_worker",
    "send_draft_worker",
    "poll_inbox_task",
    "poll_all_users_task",
    "check_upcoming_meetings_task",
    "check_all_inactivity_task",
    "reactivate_terminated_prospects_task",
    "outreach_orchestrator_worker",
    "sweep_stuck_campaigns_task",
]
