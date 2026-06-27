from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
import datetime
from datetime import UTC

from app.db.database import get_db
from app.db import models
from app.core.security import get_current_user, get_visibility_filter
from app.core.limiter import limiter
from app.core.logging_config import setup_logging
from app.core.scheduler import (
    resolve_prospect_timezone,
    calculate_next_send_slot,
    format_scheduled_display,
)
from app.services.draft_dispatch import queue_draft_dispatch, execute_draft_send

logger = setup_logging()
router = APIRouter()

class DraftUpdate(BaseModel):
    subject: str
    body: str
    email: str

def _lock_query(query):
    bind = query.session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        return query.with_for_update()
    return query

@router.get("/{draft_id}/next-slot")
def get_next_slot(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Preview the next scheduled slot for a draft WITHOUT scheduling it.
    Used by the dispatch confirmation modal to show the time before committing."""
    db_draft = (
        db.query(models.EmailDraft).join(models.Campaign).filter(
            models.EmailDraft.id == draft_id,
            get_visibility_filter(db, current_user)
        )
    ).first()
    if not db_draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    if not db_draft.dm or not db_draft.dm.email:
        raise HTTPException(status_code=400, detail="No recipient email on this draft.")

    prospect_tz = resolve_prospect_timezone(db, db_draft.dm)
    slot = calculate_next_send_slot(db_draft.campaign.user_id, prospect_tz, db)
    return {
        "scheduled_at": slot.isoformat(),
        "timezone": prospect_tz,
        "display": format_scheduled_display(slot, prospect_tz),
    }


@router.patch("/{draft_id}")
def update_draft(
    draft_id: str, 
    update: DraftUpdate, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Update a draft's subject or body before it's sent."""
    db_draft = _lock_query(
        db.query(models.EmailDraft).join(models.Campaign).filter(
            models.EmailDraft.id == draft_id,
            get_visibility_filter(db, current_user)
        )
    ).first()
    if not db_draft:
        raise HTTPException(status_code=404, detail="Email Draft not found")
    
    db_draft.subject = update.subject
    db_draft.body = update.body
    
    if db_draft.dm:
        db_draft.dm.email = update.email
        
    db.commit()
    return {"message": "Email Draft updated successfully"}


@router.post("/{draft_id}/send")
@limiter.limit("10/minute")
def send_draft(
    request: Request,
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Schedule a draft for sending at the next optimal time slot."""
    # Scope the lookup to the caller's campaigns (prevents IDOR).
    db_draft = _lock_query(
        db.query(models.EmailDraft).join(models.Campaign).filter(
            models.EmailDraft.id == draft_id,
            get_visibility_filter(db, current_user)
        )
    ).first()
    if not db_draft:
        raise HTTPException(status_code=404, detail="Email engagement protocol not found.")

    if db_draft.status == "SENT" and db_draft.message_id:
        return {"message": "Engagement protocol already deployed."}

    prospect_email = db_draft.dm.email if db_draft.dm else None
    if not prospect_email:
        raise HTTPException(status_code=400, detail="Deployment coordinate (Email) missing. Please refine and synchronize stakeholder data.")

    queue_state = queue_draft_dispatch(
        db,
        db_draft,
        queued_at=datetime.datetime.now(UTC).replace(tzinfo=None),
    )
    if queue_state == "already_sent":
        return {"message": "Engagement protocol already deployed."}
    if queue_state == "queued" and db_draft.scheduled_at:
        prospect_tz = db_draft.dm.display_timezone or "UTC"
        return {
            "message": "already_scheduled",
            "scheduled_at": db_draft.scheduled_at.isoformat(),
            "timezone": prospect_tz,
            "display": format_scheduled_display(db_draft.scheduled_at, prospect_tz),
        }
    if queue_state == "in_progress":
        raise HTTPException(status_code=429, detail="Operational Lock: This draft is currently being deployed by another worker.")
    if queue_state == "requires_review":
        raise HTTPException(
            status_code=409,
            detail="Operational Review Required: Previous deployment attempt is still unresolved for this draft."
        )

    # Resolve timezone and calculate the next available staggered slot. The draft
    # is now QUEUED with a scheduled_at in the DB; the durable dispatch poller
    # (dispatch_due_drafts_task) sends it when due. We deliberately do NOT enqueue
    # a long-lived Celery eta task here — a lost/expired eta (server down at fire
    # time) was exactly the failure that silently dropped emails. The DB schedule
    # survives restarts and the poller reconciles it.
    prospect_tz = resolve_prospect_timezone(db, db_draft.dm)
    slot = calculate_next_send_slot(db_draft.campaign.user_id, prospect_tz, db)
    db_draft.scheduled_at = slot
    db.commit()

    display = format_scheduled_display(slot, prospect_tz)
    logger.info(f"[DISPATCH] Draft {draft_id} scheduled at {slot} UTC (prospect tz: {prospect_tz})")
    return {
        "message": "scheduled",
        "scheduled_at": slot.isoformat(),
        "timezone": prospect_tz,
        "display": display,
    }


@router.post("/{draft_id}/send-now")
@limiter.limit("10/minute")
def send_draft_now(
    request: Request,
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Send a draft immediately, bypassing all scheduling, delivery windows, and queues.
    Calls the Gmail API directly in this request — no poller, no timezone constraints."""
    db_draft = _lock_query(
        db.query(models.EmailDraft).join(models.Campaign).filter(
            models.EmailDraft.id == draft_id,
            get_visibility_filter(db, current_user)
        )
    ).first()
    if not db_draft:
        raise HTTPException(status_code=404, detail="Email engagement protocol not found.")

    if db_draft.status == "SENT" and db_draft.message_id:
        return {"message": "already_sent"}

    if not db_draft.dm or not db_draft.dm.email:
        raise HTTPException(status_code=400, detail="Deployment coordinate (Email) missing.")

    if db_draft.dispatch_state == "IN_PROGRESS":
        raise HTTPException(status_code=429, detail="Operational Lock: This draft is currently being deployed.")

    # Transition to QUEUED so execute_draft_send accepts it, then commit before
    # execute_draft_send opens its own session to read the updated state.
    queue_state = queue_draft_dispatch(db, db_draft, queued_at=datetime.datetime.now(UTC).replace(tzinfo=None))
    if queue_state == "already_sent":
        return {"message": "already_sent"}
    if queue_state == "in_progress":
        raise HTTPException(status_code=429, detail="Operational Lock: This draft is currently being deployed.")
    if queue_state == "requires_review":
        raise HTTPException(status_code=409, detail="Operational Review Required: Previous deployment attempt is still unresolved.")
    db.commit()

    # execute_draft_send opens its own session — do NOT close the FastAPI session
    # manually here (double-close breaks the dependency teardown).
    result = execute_draft_send(draft_id)

    if result["status"] in ("sent", "recovered", "already_sent"):
        return {"message": "sent", "detail": result["message"]}
    if result["status"] == "in_progress":
        raise HTTPException(status_code=429, detail="Operational Lock: This draft is currently being deployed.")
    raise HTTPException(status_code=500, detail=result.get("message", "Send failed."))
