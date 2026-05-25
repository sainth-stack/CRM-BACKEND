from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request
from sqlalchemy.orm import Session, joinedload
import datetime

from app.db.database import get_db
from app.db import models
from app.core.security import get_current_user, get_visibility_filter
from app.core.scheduler import format_scheduled_display
from app.core.limiter import limiter

router = APIRouter()


def _lock_query(query):
    bind = query.session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        return query.with_for_update()
    return query


@router.get("/{dm_id}")
def get_prospect_details(
    dm_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Stakeholder Dossier Audit.
    Retrieves detailed communication history and metadata for a specific prospect.
    """
    # Sector Query Boundary: Guarantee hierarchical relationship ownership
    dm = db.query(models.DecisionMaker).join(models.Campaign).filter(
        models.DecisionMaker.id == dm_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not dm:
        raise HTTPException(status_code=404, detail="Stakeholder not found")
    
    logs = []
    for log in dm.logs:
        log_dict = log.__dict__.copy()
        log_dict.pop("_sa_instance_state", None)
        logs.append(log_dict)
        
    result = {
        "id": dm.id,
        "name": dm.name,
        "position": dm.position,
        "email": dm.email,
        "linkedin": dm.linkedin,
        "status": dm.status,
        "company_name": dm.target_company.name if dm.target_company else "N/A",
        "logs": sorted(logs, key=lambda x: x.get('received_at') or datetime.datetime.min, reverse=True)
    }

    return result


# ---------------------------------------------------------------------------
# Manual Dispatch: schedule a nudge/reminder for an active prospect
# ---------------------------------------------------------------------------

# States where the user is allowed to manually fire a nudge
_DISPATCHABLE_STATES = {
    models.ProspectState.INITIAL_SENT,
    models.ProspectState.REMINDER_1_SENT,
    models.ProspectState.FOLLOWUP_ACTIVE,
    models.ProspectState.WAITING_FOR_REPLY,
}


@router.post("/{dm_id}/dispatch")
@limiter.limit("10/minute")
def manual_dispatch_nudge(
    request: Request,
    dm_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Manually schedule a reminder / nudge for an active outreach prospect.
    Uses the same timezone-aware delivery-window logic as the automated orchestrator.
    Idempotent: returns the existing schedule if a dispatch is already queued.
    """
    # Deferred import to avoid circular startup import from Celery worker tree
    from app.workers.tasks.orchestrator_worker import _deploy_nudge, _nudge_dispatch_key

    # Auth + IDOR guard — load with all relations the nudge generator needs
    dm = (
        db.query(models.DecisionMaker)
        .options(
            joinedload(models.DecisionMaker.campaign)
            .joinedload(models.Campaign.user_intel),
            joinedload(models.DecisionMaker.campaign)
            .joinedload(models.Campaign.owner),
            joinedload(models.DecisionMaker.target_company),
        )
        .join(models.Campaign)
        .filter(
            models.DecisionMaker.id == dm_id,
            get_visibility_filter(db, current_user),
        )
        .first()
    )
    if not dm:
        raise HTTPException(status_code=404, detail="Prospect not found.")

    state = dm.state
    if state not in _DISPATCHABLE_STATES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Prospect is in state '{state.value}' — manual dispatch is only "
                "available for INITIAL_SENT, REMINDER_1_SENT, FOLLOWUP_ACTIVE, "
                "and WAITING_FOR_REPLY prospects."
            ),
        )

    # Discovery path (already waiting on a meeting confirmation)
    is_discovery = state == models.ProspectState.WAITING_FOR_REPLY
    reminder_number = dm.reminder_count + 1

    if not is_discovery and dm.reminder_count >= 2:
        raise HTTPException(
            status_code=409,
            detail="Maximum reminders (2) already sent for this prospect.",
        )

    if is_discovery:
        next_state = models.ProspectState.WAITING_FOR_REPLY
        expire_after_send = reminder_number >= 2
    else:
        next_state = (
            models.ProspectState.REMINDER_1_SENT
            if reminder_number == 1
            else models.ProspectState.REMINDER_2_SENT
        )
        expire_after_send = False

    # Idempotency guard — if already queued or in-flight, return existing slot
    dispatch_key = _nudge_dispatch_key(dm_id, reminder_number, is_discovery)
    existing = (
        db.query(models.OutboundDispatch)
        .filter(models.OutboundDispatch.dispatch_key == dispatch_key)
        .first()
    )
    if existing:
        if existing.state == "SENT":
            raise HTTPException(status_code=409, detail="This reminder has already been sent.")
        if existing.state in ("QUEUED", "IN_PROGRESS"):
            prospect_tz = dm.display_timezone or "UTC"
            disp = (
                format_scheduled_display(existing.scheduled_at, prospect_tz)
                if existing.scheduled_at
                else "Soon"
            )
            return {
                "message": "already_scheduled",
                "scheduled_at": existing.scheduled_at.isoformat() if existing.scheduled_at else None,
                "timezone": prospect_tz,
                "display": disp,
            }

    # Trigger nudge — resolves timezone, picks next window slot, schedules Celery task
    dispatched = _deploy_nudge(
        db, dm, next_state,
        reminder_number=reminder_number,
        is_discovery=is_discovery,
        expire_after_send=expire_after_send,
    )
    if not dispatched:
        raise HTTPException(
            status_code=409,
            detail="Dispatch blocked — another send is currently in progress for this prospect.",
        )

    # Pull scheduled_at from the freshly-created dispatch record
    new_dispatch = (
        db.query(models.OutboundDispatch)
        .filter(models.OutboundDispatch.dispatch_key == dispatch_key)
        .first()
    )
    prospect_tz = dm.display_timezone or "UTC"
    scheduled_at = new_dispatch.scheduled_at if new_dispatch else None
    display = format_scheduled_display(scheduled_at, prospect_tz) if scheduled_at else "Soon"

    return {
        "message": "scheduled",
        "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        "timezone": prospect_tz,
        "display": display,
    }
