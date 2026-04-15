from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, joinedload
from app.db.database import get_db, engine
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
import ipaddress
import urllib.parse
from contextlib import asynccontextmanager
from app.workers import sweep_stuck_campaigns_task
from app.core.logging_config import setup_logging
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

# Create tables if they don't exist
models.Base.metadata.create_all(bind=engine)

# --- CORS (Env-configured, production-safe) ---
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")

app = FastAPI(title="Outreach v3 API", lifespan=lifespan)

# Attach rate limiter state and error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    if current_user.role == models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Sovereign authority cannot access localized operational data.")
    elif current_user.role == models.UserRole.ADMIN:
        target_user_ids = db.query(models.User.id).filter(models.User.created_by_id == current_user.id)
        return models.Campaign.user_id.in_(target_user_ids)
    return models.Campaign.user_id == current_user.id

# --- SSRF Protection Utility ---
def validate_url_for_ssrf(url: str) -> str:
    """
    Blocks Server-Side Request Forgery attacks on user-submitted URLs.
    Rejects private IPs, loopback, cloud metadata endpoints, and non-HTTP schemes.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ["http", "https"]:
            raise HTTPException(status_code=400, detail="Only HTTP/HTTPS URLs are permitted.")
        hostname = parsed.hostname
        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid URL: missing hostname.")
        # Block raw private IP submissions
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(status_code=400, detail="Internal network URLs are not permitted.")
        except ValueError:
            pass  # Hostname string — not a raw IP, safe to proceed
        return url
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL format.")


@app.get("/health/email")
def email_health_check():
    """
    Diagnostic Sector Operations.
    Verifies the Gmail OAuth2 configuration and environmental readiness of the active sector.
    """
    import os
    config_status = {
        "GMAIL_TOKEN_JSON": "SET" if os.getenv("GMAIL_TOKEN_JSON") else "MISSING",
        "GMAIL_CREDENTIALS_JSON": "SET" if os.getenv("GMAIL_CREDENTIALS_JSON") else "MISSING",
        "EMAIL_USER": os.getenv("EMAIL_USER", "MISSING"),
        "NEON_DB_URL": "SET" if os.getenv("NEON_DB_URL") else "MISSING",
        "OPENAI_API_KEY": "SET" if os.getenv("OPENAI_API_KEY") else "MISSING",
    }
    return {"config": config_status, "mode": "STRICT_GMAIL_NATIVE"}

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
    if current_user.role != models.UserRole.USER:
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
    validated_url = validate_url_for_ssrf(campaign.user_url)

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
    """
    # N+1 Fix: Single query with all relationships eager-loaded
    db_campaign = db.query(models.Campaign).options(
        joinedload(models.Campaign.user_intel),
        joinedload(models.Campaign.target_companies),
        joinedload(models.Campaign.dms).joinedload(models.DecisionMaker.logs),
        joinedload(models.Campaign.drafts)
    ).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found in your intelligence sector")
    
    # Enrich with details based on status
    target_companies = []
    for tc in db_campaign.target_companies:
        tc_dict = tc.__dict__.copy()
        tc_dict.pop("_sa_instance_state", None)
        target_companies.append(tc_dict)
        
    dms = []
    for dm in db_campaign.dms:
        dm_dict = dm.__dict__.copy()
        dm_dict.pop("_sa_instance_state", None)
        import datetime
        
        # Include communication logs
        logs = []
        for log in dm.logs:
            ld = log.__dict__.copy()
            ld.pop("_sa_instance_state", None)
            logs.append(ld)
        dm_dict["logs"] = logs
        dms.append(dm_dict)
        
    drafts = []
    # Force Reverse-Chronological Draft Sequence (Newest first)
    from datetime import UTC
    sorted_drafts = sorted(db_campaign.drafts, key=lambda x: x.created_at if x.created_at else datetime.datetime.min.replace(tzinfo=UTC), reverse=True)
    
    for d in sorted_drafts:
        d_dict = d.__dict__.copy()
        d_dict.pop("_sa_instance_state", None)
        drafts.append(d_dict)

    result = {
        "id": db_campaign.id,
        "name": db_campaign.name,
        "status": db_campaign.status,
        "created_at": db_campaign.created_at,
        "query": db_campaign.user_query,
        "target_industry": db_campaign.target_industry,
        "target_location": db_campaign.target_location,
        "user_intel": db_campaign.user_intel.__dict__ if db_campaign.user_intel else None,
        "target_companies_count": len([c for c in db_campaign.target_companies if getattr(c, 'status', 'NEW') != "REJECTED"]),
        "target_companies": target_companies,
        "dms_count": len(db_campaign.dms),
        "dms": dms,
        "drafts_count": len(db_campaign.drafts),
        "drafts": drafts,
    }
    # Remove SQLAlchemy internal state
    if result["user_intel"]: result["user_intel"].pop("_sa_instance_state", None)
    
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
    Content Refinement Protocol.
    Updates the subject or body of an outreach draft prior to tactical deployment.
    """
    # Sector Query Boundary: Ensure draft belongs to a campaign governed by the actor
    db_draft = db.query(models.EmailDraft).join(models.Campaign).filter(
        models.EmailDraft.id == draft_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    
    db_draft.subject = update.subject
    db_draft.body = update.body
    
    if db_draft.dm:
        db_draft.dm.email = update.email
        
    db.commit()
    return {"message": "Draft updated successfully"}

@app.post("/drafts/{draft_id}/send")
@limiter.limit("10/minute")
def send_draft(
    request: Request,
    draft_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Sector Query Boundary: Hard IDOR prevention & Hierarchical Trust
    db_draft = db.query(models.EmailDraft).join(models.Campaign).filter(
        models.EmailDraft.id == draft_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_draft:
        raise HTTPException(status_code=404, detail="Email engagement protocol not found.")
    
    # 1. Coordinate Validation
    prospect_email = db_draft.dm.email if db_draft.dm else None
    if not prospect_email:
        raise HTTPException(status_code=400, detail="Deployment coordinate (Email) missing. Please refine and synchronize stakeholder data.")
        
    from app.core.email_service import email_service
    
    try:
        # 2. Coordinate Threading
        thread_id = db_draft.dm.thread_id
        
        from app.core.token_service import TokenService
        # 3. Strategic Deployment with campaign-specific credentials
        creds = TokenService.get_google_credentials(db, db_draft.campaign.user_id)
        msg_data = email_service.send_email(
            to_email=prospect_email,
            subject=db_draft.subject,
            body=db_draft.body,
            creds=creds,
            thread_id=thread_id
        )
        msg_id = msg_data["id"]
        thread_id = msg_data["thread_id"]
        
        # 4. Log to Communication History
        from datetime import UTC
        import datetime
        new_log = models.CommunicationLog(
            campaign_id=db_draft.campaign_id,
            dm_id=db_draft.decision_maker_id,
            direction="SENT",
            subject=db_draft.subject,
            body=db_draft.body,
            message_id=msg_id
        )
        db.add(new_log)
        
        # 5. Status & CRM Synchronization
        if db_draft.dm:
            dm = db_draft.dm
            dm.last_message_id = msg_id
            dm.thread_id = thread_id
            if dm.status == "DISCOVERY_CALL":
                hs_status = "Discovery Invitation Sent"
                dm.status = "WAITING_FOR_REPLY" # Shared state for 'Wait' but UI will use pulse logic
            elif db_draft.followup_index == 0:
                hs_status = "Initial Email Sent"
                dm.status = "INITIAL_SENT"
            else:
                idx = db_draft.followup_index
                dm.followup_count = idx
                suffix = "st" if idx == 1 else "nd" if idx == 2 else "rd" if idx == 3 else "th"
                hs_status = f"{idx}{suffix} Follow Up Sent"
                dm.status = f"FOLLOWUP_{idx}_SENT"
            
            # Update HubSpot CRM
            from app.integrations.hubspot import hubspot_provider
            hubspot_provider.update_lead_status(dm.hubspot_id, hs_status)
            
        db_draft.status = "SENT"
        db_draft.message_id = msg_id
        db_draft.sent_at = datetime.datetime.now(UTC)
            
        db.commit()
        return {"message": f"Engagement protocol mobilized. HubSpot status updated to '{hs_status if db_draft.dm else 'N/A'}'."}
    except Exception as e:
        logger.error(f"Tactical Deployment Failure for draft {draft_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Tactical deployment failed: {str(e)}")

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
