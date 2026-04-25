from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, joinedload
from app.db.database import get_db
from app.db import models
from app.api import auth
from app.core.security import get_current_user
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
import uuid
import datetime
from datetime import UTC
import ipaddress
import urllib.parse
from contextlib import asynccontextmanager
from app.workers import sweep_stuck_campaigns_task
from app.core.logging_config import setup_logging
from app.services.draft_dispatch import (
    _apply_sent_draft_effects,
    queue_draft_dispatch,
)
from app.workers.tasks.outbound_worker import send_draft_worker
import gc
import os

# Initialize Enterprise Logging
logger = setup_logging()

# --- Rate Limiter (IP-based, Cloud-backed for scalability) ---
limiter = Limiter(key_func=get_remote_address, storage_uri=os.getenv("REDIS_URL"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Startup: Resurrection Protocol
    # Scans for campaigns that were processing when server last died
    logger.info("Mobilizing Outreach Resurrection Protocol...")
    sweep_stuck_campaigns_task()
    yield
    # 2. Shutdown: Cleanup
    logger.info("Decommissioning API server... performing memory sweep.")
    gc.collect()

app = FastAPI(title="Outreach v3 API", lifespan=lifespan)

# --- Observability Middleware ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"[GATEWAY] Incoming {request.method} request to {request.url.path}")
    response = await call_next(request)
    logger.info(f"[GATEWAY] Completed {request.method} {request.url.path} | Status: {response.status_code}")
    return response

# --- CORS (Env-configured, production-safe) ---
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
ALLOWED_ORIGINS = [FRONTEND_URL, "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Attach rate limiter state and error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth.router)
app.include_router(auth.capability_router)

class CampaignCreate(BaseModel):
    name: str
    user_url: str
    target_industry: str
    target_location: str
    target_employee_count: str | None = None
    query: str = "" # Keeping as optional for legacy if needed, but industry/location are primary now
    class Config:
        from_attributes = True

class CampaignResponse(BaseModel):
    id: str
    name: str
    status: models.CampaignStatus
    created_at: datetime.datetime
    class Config:
        from_attributes = True

class BatchDeleteRequest(BaseModel):
    campaign_ids: list[str]

def get_visibility_filter(db: Session, current_user: models.User):
    """Enforces the Zero-Trust multi-tenant data isolation boundary."""
    if str(current_user.role).lower().split('.')[-1] == "super_admin":
        raise HTTPException(status_code=403, detail="Sovereign authority cannot access localized operational data.")
    elif str(current_user.role).lower().split('.')[-1] == "admin":
        target_user_ids = db.query(models.User.id).filter(models.User.created_by_id == current_user.id)
        return models.Campaign.user_id.in_(target_user_ids)
    return models.Campaign.user_id == current_user.id


def _lock_query(query):
    bind = query.session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        return query.with_for_update()
    return query

# --- SSRF Protection Utility ---
from app.core.security import validate_url_for_ssrf


@app.get("/health/email")
def email_health_check(current_user: models.User = Depends(get_current_user)):
    """
    Sector Health Status.
    Authorized diagnostic check. Limited to Super Admins to prevent infrastructure metadata leakage.
    """
    if current_user.role != models.UserRole.SUPER_ADMIN:
        return {"status": "OK", "mode": "STRICT_GMAIL_NATIVE"}

    import os
    config_status = {
        "GMAIL_TOKEN_JSON": "SET" if os.getenv("GMAIL_TOKEN_JSON") else "MISSING",
        "GMAIL_CREDENTIALS_JSON": "SET" if os.getenv("GMAIL_CREDENTIALS_JSON") else "MISSING",
        "EMAIL_USER": "CONFIGURED" if os.getenv("EMAIL_USER") else "MISSING",
        "NEON_DB_URL": "CONNECTED" if os.getenv("NEON_DB_URL") else "MISSING",
        "REDIS_INFRA": "ACTIVE" if os.getenv("REDIS_URL") else "FALLBACK_LOCAL"
    }
    
    # Live Ping
    from app.core.security import _get_redis
    r = _get_redis()
    config_status["REDIS_LIVENESS"] = "PONG" if r else "UNREACHABLE"
    
    return {"status": "HEALTHY", "config": config_status}


@app.get("/health/dependencies")
def dependency_health_check(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Dependency health snapshot for operators.
    Gives the current actor a safe readout of the backing services that drive outreach execution.
    """
    from app.core.security import _get_redis

    mailbox_connected = (
        db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == current_user.id).first() is not None
    )
    redis_client = _get_redis()

    payload = {
        "status": "healthy" if redis_client else "degraded",
        "dependencies": {
            "redis": "up" if redis_client else "down",
            "gmail_mailbox": "connected" if mailbox_connected else "disconnected",
            "gmail_system_vault": "configured" if os.getenv("GMAIL_TOKEN_JSON") else "missing",
            "hubspot": "configured" if os.getenv("HUBSPOT_ACCESS_TOKEN") else "missing",
            "cal": "configured" if os.getenv("CAL_API_KEY") and os.getenv("CAL_EVENT_TYPE_ID") else "missing",
        },
    }

    if current_user.role == models.UserRole.SUPER_ADMIN:
        payload["environment"] = {
            "frontend_url": "configured" if os.getenv("FRONTEND_URL") else "default",
            "neon_db_url": "configured" if os.getenv("NEON_DB_URL") else "missing",
        }

    return payload

from app.core.sanitizer import sanitize_text

@app.post("/campaigns", response_model=CampaignResponse)
@limiter.limit("5/minute")
def create_campaign(
    request: Request,
    campaign: CampaignCreate, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Campaign Mobilization Protocol.
    Initializes a new outreach campaign, validates target parameters, and triggers the autonomous research cluster.
    Input parameters are normalized via the Sanitization Engine to mitigate injection risks.
    """
    # Hierarchy boundary: Only localized operators can initialize executions
    if str(current_user.role).lower().split('.')[-1] != "user":
        raise HTTPException(status_code=403, detail="Only Localized Users can mobilize new campaigns.")

    # 0. High-Fidelity Input Sanitization
    sanitized_name = sanitize_text(campaign.name, max_length=100)
    sanitized_industry = sanitize_text(campaign.target_industry, max_length=200)
    sanitized_location = sanitize_text(campaign.target_location, max_length=200)
    sanitized_emp_count = sanitize_text(campaign.target_employee_count, max_length=50) if campaign.target_employee_count else None
    
    # Tactical Limit Enforcement for Demo Identities (Permanent Lock)
    if current_user.is_demo:
        # We use a persistent sentinel to ensure deletion doesn't reset the quota
        if current_user.has_used_trial_quota:
            raise HTTPException(
                status_code=403, 
                detail="Trial identity quota exceeded. You have already utilized your 1-campaign entitlement. Please upgrade to professional access for unlimited mobilization."
            )

    # 1. Create campaign in DB tied to user
    campaign_id = str(uuid.uuid4())
    new_campaign = models.Campaign(
        id=campaign_id,
        user_id=current_user.id,
        name=sanitized_name,
        user_query=sanitize_text(campaign.query, max_length=500),
        target_industry=sanitized_industry,
        target_location=sanitized_location,
        target_employee_count=sanitized_emp_count,
        status=models.CampaignStatus.RESEARCHING_USER_COMPANY
    )
    db.add(new_campaign)
    
    # Commit quota consumption for Demo Users
    if current_user.is_demo:
        # Atomic lock constraint: Prevents multi-click race conditions entirely
        updated_rows = db.query(models.User).filter(
            models.User.id == current_user.id,
            models.User.has_used_trial_quota.in_([False, None])
        ).update({"has_used_trial_quota": True}, synchronize_session=False)
        
        if updated_rows == 0:
            db.rollback()
            raise HTTPException(
                status_code=429, 
                detail="Deployment Lock: Your 1-campaign entitlement has been strictly enforced. Simultaneous operation intercepted."
            )
    
    # Apply SSRF protection on user-submitted URL
    raw_url = campaign.user_url.strip()
    if not raw_url.startswith(("http://", "https://")):
        raw_url = f"https://{raw_url}"
    validated_url, _ = validate_url_for_ssrf(raw_url)

    # 2. Instantly persist Mission Origin
    intel = models.UserCompanyIntel(
        campaign_id=campaign_id,
        website=validated_url,
        company_name="Synchronizing Identity..."
    )
    db.add(intel)
    db.commit()
    db.refresh(new_campaign)
    
    # 3. Trigger Stage 1 via Distributed Celery Message Queue
    from app.workers import research_user_company_worker
    research_user_company_worker.delay(campaign_id)
    
    return new_campaign

@app.get("/campaigns")
def list_campaigns(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page")
):
    """
    Sovereign Campaign Audit.
    Retrieves a paginated list of campaigns governed by the actor, enforcing strict multi-tenant isolation boundaries.
    """
    visibility_filter = get_visibility_filter(db, current_user)
    skip = (page - 1) * page_size

    total = db.query(models.Campaign).filter(visibility_filter).count()
    db_campaigns = db.query(models.Campaign).filter(
        visibility_filter
    ).order_by(models.Campaign.created_at.desc()).offset(skip).limit(page_size).all()

    results = []
    for campaign in db_campaigns:
        results.append({
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
            "created_at": campaign.created_at,
            "query": campaign.user_query,
            "target_industry": campaign.target_industry,
            "target_location": campaign.target_location,
        })
    return {
        "campaigns": results,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size
    }

@app.delete("/campaigns/{campaign_id}")
def delete_campaign(
    campaign_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Sector Asset Decommissioning.
    Permanently deletes a campaign and its associated intelligence data from the sector.
    """
    # Isolation: Apply Zero-Trust boundary
    db_campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found or access denied")
    
    db.delete(db_campaign)
    db.commit()
    return {"message": "Campaign deleted successfully"}

@app.patch("/campaigns/{campaign_id}/status", response_model=CampaignResponse)
@app.put("/campaigns/{campaign_id}/status", response_model=CampaignResponse)
@limiter.limit("15/minute")
def update_campaign_status(
    request: Request,
    campaign_id: str, 
    status: str = Query(..., alias="status"), 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Operational State Redirection.
    Manually overrides the functional state of a campaign to facilitate human-in-the-loop intervention.
    """
    db_campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found or access denied")
    
    try:
        # Map string status to Enum member
        status_enum = models.CampaignStatus(status.upper())
        db_campaign.status = status_enum
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    db.commit()
    db.refresh(db_campaign)
    return db_campaign

@app.post("/campaigns/batch-delete")
def batch_delete_campaigns(
    request: BatchDeleteRequest, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Bulk Intelligence Termination.
    Efficiently decommissions multiple campaigns simultaneously while maintaining sector integrity.
    """
    campaign_ids = request.campaign_ids
    # Multi-tenant policy: hierarchical batch isolation
    campaigns_to_delete = db.query(models.Campaign).filter(
        models.Campaign.id.in_(campaign_ids),
        get_visibility_filter(db, current_user)
    ).all()
    
    count = len(campaigns_to_delete)
    if count == 0:
        return {"message": "No campaigns identified for decommission within your sector."}

    # Bulk deletion with hierarchical boundary check
    db.query(models.Campaign).filter(
        models.Campaign.id.in_(campaign_ids),
        get_visibility_filter(db, current_user)
    ).delete(synchronize_session=False)
    db.commit()
    return {"message": f"Successfully decommissioned {count} campaigns from your intelligence sector."}

@app.get("/campaigns/{campaign_id}")
def get_campaign(
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Deep Intelligence Audit.
    Retrieves comprehensive metadata, discovered stakeholders, and communication history for a specific campaign.
    """    # N+1 Fix: Single query with all relationships eager-loaded
    db_campaign = db.query(models.Campaign).options(
        joinedload(models.Campaign.user_intel),
        joinedload(models.Campaign.target_companies),
        joinedload(models.Campaign.dms).joinedload(models.DecisionMaker.logs),
        joinedload(models.Campaign.dms).joinedload(models.DecisionMaker.drafts),
        joinedload(models.Campaign.dms).joinedload(models.DecisionMaker.transitions),
    ).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found in your intelligence sector")
    
    # 1. Map Target Companies
    target_companies = []
    for tc in db_campaign.target_companies:
        tc_dict = tc.__dict__.copy()
        tc_dict.pop("_sa_instance_state", None)
        target_companies.append(tc_dict)
        
    # 2. Cluster Outbound Protocols (Nesting Drafts under DMs)
    dms = []
    for dm in db_campaign.dms:
        dm_dict = dm.__dict__.copy()
        dm_dict.pop("_sa_instance_state", None)
        
        # A. Attach Communication Logs
        dm_dict["logs"] = [
            {k: v for k, v in log.__dict__.items() if k != "_sa_instance_state"} 
            for log in sorted(dm.logs, key=lambda l: l.received_at or datetime.datetime.min, reverse=True)
        ]
        
        # B. Attach Channels Specific Drafts (Taking newest only)
        email_draft = sorted(dm.drafts, key=lambda x: x.created_at or datetime.datetime.min, reverse=True)[0] if dm.drafts else None
        dm_dict["email_draft"] = {k: v for k, v in email_draft.__dict__.items() if k != "_sa_instance_state"} if email_draft else None
        
        # C. Strategic Recommendation Engine (V8 Consolidated Email Pattern)
        recommendation = { "channel": "email", "reason": "System initializing..." }
        
        # D. High-Fidelity Lifecycle Timeline Construction
        timeline = []
        state = dm.state or models.ProspectState.NEW
        
        # Build deterministic timeline segments based on state machine position
        timeline.append({"step": "Research", "status": "done"})
        
        if state == models.ProspectState.NEW:
            timeline.append({"step": "Outreach Initialization", "status": "active"})
        elif state == models.ProspectState.DRAFTED:
            timeline.append({"step": "Initial Outreach", "status": "active"})
        else:
            timeline.append({"step": "Initial Outreach", "status": "done"})

        if dm.reminder_count > 0:
            timeline.append({"step": f"Reminder {dm.reminder_count}", "status": "done"})

        if state == models.ProspectState.ON_HOLD:
            timeline.append({"step": "On Hold", "status": "active"})

        if state in [
            models.ProspectState.INITIAL_SENT,
            models.ProspectState.WAITING_FOR_REPLY,
            models.ProspectState.REMINDER_1_SENT,
            models.ProspectState.REMINDER_2_SENT,
            models.ProspectState.FOLLOWUP_ACTIVE,
        ]:
            timeline.append({"step": "Waiting for Reply", "status": "active"})

        if state == models.ProspectState.NEUTRAL:
            timeline.append({"step": "Neutral Reply", "status": "active"})

        if dm.followup_count > 0:
            timeline.append({"step": f"Follow-up {dm.followup_count}", "status": "done"})

        if state in [models.ProspectState.DISCOVERY_CALL, models.ProspectState.WAITING_FOR_REPLY]:
            timeline.append({"step": "Positive Intent Detected", "status": "done"})
            timeline.append({"step": "Discovery Call Coordination", "status": "active"})
        elif state == models.ProspectState.DISCOVERY_EXPIRED:
            timeline.append({"step": "Positive Intent Detected", "status": "done"})
            timeline.append({"step": "Discovery Call Coordination", "status": "done"})
            timeline.append({"step": "Discovery Expired", "status": "active"})
        elif state == models.ProspectState.MEETING_BOOKED:
            timeline.append({"step": "Positive Intent Detected", "status": "done"})
            timeline.append({"step": "Discovery Call Coordination", "status": "done"})
            timeline.append({"step": "Meeting Scheduled", "status": "done"})
        elif state == models.ProspectState.TERMINATED:
            timeline.append({"step": "Terminated", "status": "error"})
            if dm.retry_after and dm.termination_reason != models.ProspectTerminationReason.INTERNAL_LEAD_SECURED:
                timeline.append({"step": "Retry Queue", "status": "active"})

        dm_dict["timeline"] = timeline
        dm_dict["state"] = state
        dm_dict["intent"] = dm.intent_last
        dm_dict["next_action_at"] = dm.next_action_at
        dm_dict["retry_after"] = dm.retry_after
        dm_dict["hold_release_at"] = dm.hold_release_at
        dm_dict["hold_source_dm_id"] = dm.hold_source_dm_id
        dm_dict["termination_reason"] = dm.termination_reason.value if dm.termination_reason else None
        dm_dict["scheduled_time"] = dm.scheduled_time_utc
        dm_dict["transition_events"] = [
            {
                "from_state": tr.from_state.value if tr.from_state else None,
                "to_state": tr.to_state.value if tr.to_state else None,
                "from_status": tr.from_status,
                "to_status": tr.to_status,
                "reason": tr.reason,
                "actor": tr.actor,
                "created_at": tr.created_at,
            }
            for tr in sorted(dm.transitions, key=lambda t: t.created_at or datetime.datetime.min, reverse=True)[:10]
        ]

        if email_draft:
            recommendation = {
                "channel": "email",
                "reason": f"Active protocol: {state.value if hasattr(state, 'value') else state}"
            }
        else:
             recommendation = {
                "channel": "email",
                "reason": "Awaiting protocol generation..."
            }
            
        dm_dict["outreach_recommendation"] = recommendation
        dms.append(dm_dict)
        
    result = {
        "id": db_campaign.id,
        "name": db_campaign.name,
        "status": db_campaign.status,
        "created_at": db_campaign.created_at,
        "query": db_campaign.user_query,
        "target_industry": db_campaign.target_industry,
        "target_location": db_campaign.target_location,
        "user_intel": {k: v for k, v in db_campaign.user_intel.__dict__.items() if k != "_sa_instance_state"} if db_campaign.user_intel else None,
        "target_companies_count": len([c for c in db_campaign.target_companies if getattr(c, 'status', 'NEW') != "REJECTED"]),
        "target_companies": target_companies,
        "dms_count": len(db_campaign.dms),
        "dms": dms,
        "drafts": sorted(
            [
                {k: v for k, v in draft.__dict__.items() if k != "_sa_instance_state"}
                for dm in db_campaign.dms
                for draft in dm.drafts
            ],
            key=lambda x: x.get("created_at") or datetime.datetime.min,
            reverse=True,
        ),
        "drafts_count": sum(len(dm.drafts) for dm in db_campaign.dms),
    }
    
    return result

class DraftUpdate(BaseModel):
    subject: str
    body: str
    email: str

@app.patch("/drafts/{draft_id}")
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

@app.post("/drafts/{draft_id}/approve")
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

@app.post("/drafts/{draft_id}/send")
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

@app.get("/prospects/{dm_id}")
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
        "logs": sorted(logs, key=lambda x: x['received_at'], reverse=True)
    }
    
    return result

@app.get("/health")
def health():
    """System Vitality Check."""
    return {"status": "ok"}
