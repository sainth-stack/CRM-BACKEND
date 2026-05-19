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
from app.services.draft_dispatch import queue_draft_dispatch
from app.workers.tasks.outbound_worker import send_draft_worker

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

@router.patch("/{draft_id}")
def update_draft(
    draft_id: str, 
    update: DraftUpdate, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Email Protocol Refinement.
    Updates the subject or body of an outreach draft prior to tactical deployment.
    """
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


@router.post("/{draft_id}/approve")
def approve_draft(
    draft_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Engagement Protocol Authorization.
    Authorizes a specific draft for tactical deployment.
    """
    db_draft = db.query(models.EmailDraft).join(models.Campaign).filter(
        models.EmailDraft.id == draft_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_draft:
        raise HTTPException(status_code=404, detail="Email Draft not found")
    
    db_draft.is_approved = True
    db.commit()
    return {"message": "Email Draft authorized for deployment."}


@router.post("/{draft_id}/send")
@limiter.limit("10/minute")
def send_draft(
    request: Request,
    draft_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Sector Query Boundary: Hard IDOR prevention & Hierarchical Trust
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
    
    # 1. Coordinate Validation
    prospect_email = db_draft.dm.email if db_draft.dm else None
    if not prospect_email:
        raise HTTPException(status_code=400, detail="Deployment coordinate (Email) missing. Please refine and synchronize stakeholder data.")
        
    # 1.1 Approval Boundary Enforcement
    if not db_draft.is_approved:
        raise HTTPException(status_code=403, detail="Operational Gate: Explicit approval required prior to live email engagement.")

    queue_state = queue_draft_dispatch(
        db,
        db_draft,
        queued_at=datetime.datetime.now(UTC).replace(tzinfo=None),
    )
    if queue_state == "already_sent":
        return {"message": "Engagement protocol already deployed."}
    if queue_state == "in_progress":
        raise HTTPException(status_code=429, detail="Operational Lock: This draft is currently being deployed by another worker.")
    if queue_state == "requires_review":
        raise HTTPException(
            status_code=409,
            detail="Operational Review Required: Previous deployment attempt is still unresolved for this draft."
        )

    db.commit()
    try:
        send_draft_worker.delay(draft_id)
    except Exception as exc:
        db.rollback()
        recovery_draft = _lock_query(
            db.query(models.EmailDraft).join(models.Campaign).filter(
                models.EmailDraft.id == draft_id,
                get_visibility_filter(db, current_user)
            )
        ).first()
        if recovery_draft and recovery_draft.status != "SENT":
            recovery_draft.dispatch_state = "FAILED"
            recovery_draft.dispatch_error = f"Failed to enqueue outbound delivery: {str(exc)}"[:1000]
            db.commit()
        logger.error(f"[DISPATCH] Failed to enqueue draft {draft_id}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Outbound dispatch infrastructure is temporarily unavailable. Please retry shortly.",
        )
    return {"message": "Engagement protocol queued for deployment."}
