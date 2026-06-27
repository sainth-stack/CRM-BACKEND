from fastapi import APIRouter, Depends, HTTPException, Query, Form, File, UploadFile, Response
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from sqlalchemy.orm import Session, selectinload
from pydantic import BaseModel
import json
import uuid
import datetime
from datetime import UTC

from app.db.database import get_db
from app.db import models
from app.core.security import get_current_user, get_visibility_filter, validate_url_for_ssrf
from app.core.limiter import limiter
from app.core.logging_config import setup_logging
from app.core.sanitizer import sanitize_text
from app.core.config import settings
from app.services.input_validation_service import input_validation_service
from app.workers.tasks.intel_worker import process_csv_worker

logger = setup_logging()
router = APIRouter()

class CampaignCreate(BaseModel):
    name: str
    user_url: str
    target_industry: str
    target_location: str
    target_employee_count: str
    prompt: str
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

class CampaignUpdate(BaseModel):
    prompt: str | None = None
    target_industry: str | None = None
    target_location: str | None = None
    name: str | None = None

def _lock_query(query):
    bind = query.session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        return query.with_for_update()
    return query


@router.post("")
@limiter.limit("5/minute")
def create_campaign(
    request: Request,
    name: str = Form(...),
    user_url: str = Form(...),
    target_industry: str = Form(...),
    target_location: str = Form(...),
    target_employee_count: str = Form(...),
    prompt: str = Form(...),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Stage 1: input validation.
    Creates a campaign only after the user-provided setup inputs pass validation.
    All inputs are sanitized to mitigate injection risks.
    """
    # Any authenticated role (user, admin, super_admin) may start a campaign — it is
    # always owned by current_user.id (their own account / mailbox), so admins and
    # super admins start campaigns under themselves, not on behalf of other users.

    # 0. Sanitize inputs.
    sanitized_name = sanitize_text(name, max_length=100)
    # Lengths accommodate comma-separated multi-values (industry/location/size).
    sanitized_industry = sanitize_text(target_industry, max_length=300)
    sanitized_location = sanitize_text(target_location, max_length=300)
    sanitized_emp_count = sanitize_text(target_employee_count, max_length=120)
    sanitized_prompt = sanitize_text(prompt, max_length=2000)
    
    # 0.1 Required-field checks.
    if not sanitized_name:
        raise HTTPException(status_code=400, detail="Campaign name is required.")
    if not file:
        raise HTTPException(status_code=400, detail="A lead CSV file is required.")
    if not user_url:
        raise HTTPException(status_code=400, detail="A company URL is required.")
    if not sanitized_industry:
        raise HTTPException(status_code=400, detail="Target industry is required.")
    if not sanitized_location:
        raise HTTPException(status_code=400, detail="Target location is required.")
    if len(sanitized_industry) < 2:
        raise HTTPException(status_code=400, detail="Target industry must be at least 2 characters.")
    if len(sanitized_location) < 2:
        raise HTTPException(status_code=400, detail="Target location must be at least 2 characters.")
    if not sanitized_emp_count:
        raise HTTPException(status_code=400, detail="Target employee count is required.")
    if not sanitized_prompt:
        raise HTTPException(status_code=400, detail="Campaign prompt is required.")

    if not file.filename.lower().endswith((".csv", ".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only CSV or Excel (.xlsx, .xls) files are accepted.")

    # 0.2 URL Normalization + SSRF Protection
    raw_url = user_url.strip()
    if not raw_url.startswith(("http://", "https://")):
        raw_url = f"https://{raw_url}"
    safe_url, _validated_ip = validate_url_for_ssrf(raw_url)

    # 0.3 Strict LLM Input Review
    input_review = input_validation_service.review_inputs(
        target_industry=sanitized_industry,
        target_location=sanitized_location,
        prompt=sanitized_prompt,
        sender_website=safe_url,
    )

    # Safety net: user_url is mandatory and the offering is sourced from the website
    # by Track B (brand research) — so any clarification question asking "what do you
    # offer / what product / what service" is redundant. Filter those out; if nothing
    # remains, treat the review as success (the LLM over-triggered on offering).
    _OFFERING_KEYWORDS = ("offer", "product", "service", "solution", "deliverable", "what do you sell")
    if input_review.overall.requires_user_clarification:
        real_qs = [
            q for q in (input_review.overall.clarification_questions or [])
            if not any(k in q.lower() for k in _OFFERING_KEYWORDS)
        ]
        if not real_qs:
            input_review.overall.status = "success"
            input_review.overall.requires_user_clarification = False
            input_review.overall.clarification_questions = []
            # Also clear the per-field flag if it was only about prompt-offering.
            input_review.prompt.clarification_needed = False
        else:
            input_review.overall.clarification_questions = real_qs

    if input_review.overall.status == "needs_clarification" or input_review.overall.requires_user_clarification:
        return {
            "stage": "input_validation",
            "status": "needs_clarification",
            "clarification_questions": input_review.overall.clarification_questions,
            "fields_requiring_clarification": [
                field_name
                for field_name, field_review in [
                    ("target_industry", input_review.target_industry),
                    ("target_location", input_review.target_location),
                    ("prompt", input_review.prompt),
                ]
                if field_review.clarification_needed
            ],
            "ready_for_next_stage": False,
        }

    reviewed_industry = sanitize_text(input_review.target_industry.corrected, max_length=200) or sanitized_industry
    reviewed_location = sanitize_text(input_review.target_location.corrected, max_length=200) or sanitized_location
    # F5: prompt is mandatory — never persist a blank. If the reviewer returns an
    # empty enhanced prompt, fall back to the user's sanitized original.
    reviewed_prompt = sanitize_text(input_review.prompt.enhanced, max_length=2000) if input_review.prompt.enhanced else sanitized_prompt

    # 1. Create campaign in DB tied to user
    campaign_id = str(uuid.uuid4())
    
    # 2. Stream-trim the CSV to MAX_CSV_ROWS.
    MAX_FILE_SIZE = 200 * 1024 * 1024 # 200MB Limit

    try:
        # Check size before reading (cheap seek/tell, no data loaded).
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)

        if file_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="CSV file must be under 200MB.")

        # F2: stream-trim straight from the upload handle. pandas reads only
        # MAX_CSV_ROWS and stops — the whole file is never pulled into RAM (no
        # file.file.read(), no BytesIO copy). Bounds ingestion memory + latency.
        # Lazy import: pandas costs ~3s to import, so keep it off the API's
        # startup path — only pay it on a request that actually uploads a CSV.
        from app.services.csv_service import CSVProcessingService
        csv_svc = CSVProcessingService()
        # xlsx/xls inputs are converted to CSV bytes RIGHT HERE so every line of the
        # downstream pipeline (trim -> parse -> persist) treats the upload as CSV with
        # zero special-casing. Excel cannot be true-streamed so we read the first sheet
        # of the workbook once (capped to MAX_CSV_ROWS) and hand the resulting CSV to
        # the regular trim path as if the user had uploaded CSV in the first place.
        upload_fh = file.file
        if file.filename.lower().endswith((".xlsx", ".xls")):
            import io, pandas as pd
            engine = "openpyxl" if file.filename.lower().endswith(".xlsx") else None
            upload_fh.seek(0)
            df_xl = pd.read_excel(upload_fh, sheet_name=0, nrows=settings.MAX_CSV_ROWS, engine=engine)
            upload_fh = io.BytesIO(df_xl.to_csv(index=False).encode("utf-8"))

        trimmed_content = csv_svc.trim_csv_from_filelike(upload_fh, max_rows=settings.MAX_CSV_ROWS)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Could not process CSV for campaign {campaign_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to process the uploaded CSV.")

    # F1: the trimmed CSV is persisted ONLY to the DB (single SSoT). No local
    # uploads/ file is written — removes the orphan-file disk leak and the
    # local-storage dependency. csv_file_url stays NULL.
    new_campaign = models.Campaign(
        id=campaign_id,
        user_id=current_user.id,
        name=sanitized_name,
        prompt=reviewed_prompt,
        input_validation_review=input_review.model_dump(),
        target_industry=reviewed_industry,
        target_location=reviewed_location,
        target_employee_count=sanitized_emp_count,
        trimmed_csv_data=trimmed_content, # DB SSoT (only copy)
        status=models.CampaignStatus.INPUT_VALIDATED
    )
    db.add(new_campaign)
    
    # Consume the one-campaign trial quota for demo users.
    if current_user.is_demo:
        # Atomic update guards against double-submit race conditions.
        updated_rows = db.query(models.User).filter(
            models.User.id == current_user.id,
            models.User.has_used_trial_quota.in_([False, None])
        ).update({"has_used_trial_quota": True}, synchronize_session=False)
        
        if updated_rows == 0:
            db.rollback()
            raise HTTPException(
                status_code=429, 
                detail="Your trial includes a single campaign, which has already been used."
            )
    
    intel_id = str(uuid.uuid4())
    new_intel = models.UserCompanyIntel(
        id=intel_id,
        campaign_id=campaign_id,
        company_name=sanitized_name,
        website=safe_url,
        motto="",
        offerings="",
        deep_research=""
    )
    db.add(new_intel)
    db.commit()

    # 3. Kick off the background pipeline.
    # Input validation already ran synchronously above (single validator) and its
    # review is persisted, so only the CSV + brand-intel tracks need dispatching;
    # the persisted review already satisfies the pipeline's val_done gate.
    process_csv_worker.delay(campaign_id)

    return {
        "id": campaign_id,
        "name": new_campaign.name,
        "status": new_campaign.status,
        "created_at": new_campaign.created_at,
        "message": "Campaign inputs validated and saved successfully."
    }


@router.get("")
def get_campaigns(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page")
):
    """
    List the caller's campaigns (paginated), scoped to what they're allowed to see.
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


@router.delete("/{campaign_id}")
def delete_campaign(
    campaign_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Permanently delete a campaign and all of its associated data.
    """
    # Enforce the caller's visibility scope.
    db_campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found or access denied")
    
    db.delete(db_campaign)
    db.commit()
    return {"message": "Campaign deleted successfully"}


@router.patch("/{campaign_id}/status", response_model=CampaignResponse)
@router.put("/{campaign_id}/status", response_model=CampaignResponse)
@limiter.limit("15/minute")
def update_campaign_status(
    request: Request,
    campaign_id: str, 
    status: str = Query(..., alias="status"), 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Manually override a campaign's status (human-in-the-loop).
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


@router.post("/batch-delete")
def batch_delete_campaigns(
    request: BatchDeleteRequest, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Delete multiple campaigns in a single request.
    """
    campaign_ids = request.campaign_ids
    # Scope to campaigns the caller is allowed to delete.
    campaigns_to_delete = db.query(models.Campaign).filter(
        models.Campaign.id.in_(campaign_ids),
        get_visibility_filter(db, current_user)
    ).all()
    
    count = len(campaigns_to_delete)
    if count == 0:
        return {"message": "No matching campaigns found."}

    # Bulk delete within the caller's scope.
    db.query(models.Campaign).filter(
        models.Campaign.id.in_(campaign_ids),
        get_visibility_filter(db, current_user)
    ).delete(synchronize_session=False)
    db.commit()
    return {"message": f"Deleted {count} campaign(s)."}


@router.get("/{campaign_id}")
def get_campaign(
    request: Request,
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Return a campaign's full detail: metadata, discovered stakeholders, and communication history.
    """
    from app.core.cache import campaign_detail_key, cache_get, cache_set, CAMPAIGN_DETAIL_TTL, campaign_generation

    # Cache fast-path: a cheap access check (no graph load) enforces visibility, then
    # we serve the prebuilt JSON straight from Redis — skipping the full eager-load +
    # dict materialization that the poll would otherwise repeat every time.
    access = db.query(models.Campaign.id).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user),
    ).first()
    if not access:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    # Conditional GET: the per-campaign generation counter is the ETag. If the client
    # already holds the current version, reply 304 (no payload built or sent, and no
    # Postgres reload). `no-cache` makes the browser revalidate (send If-None-Match)
    # on each poll rather than re-download an unchanged body.
    etag = f'W/"{campaign_id}.{campaign_generation(campaign_id)}"'
    _headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=_headers)

    _cache_key = campaign_detail_key(campaign_id)
    _cached = cache_get(_cache_key)
    if _cached is not None:
        return Response(content=_cached, media_type="application/json", headers=_headers)

    # N+1 Fix: Single query with all relationships eager-loaded
    db_campaign = db.query(models.Campaign).options(
        selectinload(models.Campaign.user_intel),
        selectinload(models.Campaign.target_companies),
        selectinload(models.Campaign.dms).selectinload(models.DecisionMaker.logs),
        selectinload(models.Campaign.dms).selectinload(models.DecisionMaker.drafts),
        selectinload(models.Campaign.dms).selectinload(models.DecisionMaker.transitions),
    ).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    
    # 1. Map Target Companies
    target_companies = []
    for tc in db_campaign.target_companies:
        tc_dict = tc.__dict__.copy()
        tc_dict.pop("_sa_instance_state", None)
        target_companies.append(tc_dict)
        
    # 2. Group drafts under their decision makers.
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
            models.ProspectState.REMINDER_3_SENT,
            models.ProspectState.REMINDER_4_SENT,
            models.ProspectState.REMINDER_5_SENT,
            models.ProspectState.REMINDER_6_SENT,
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
                "reason": f"Current state: {state.value if hasattr(state, 'value') else state}"
            }
        else:
             recommendation = {
                "channel": "email",
                "reason": "Awaiting draft generation..."
            }
            
        dm_dict["outreach_recommendation"] = recommendation
        dms.append(dm_dict)
        
    result = {
        "id": db_campaign.id,
        "name": db_campaign.name,
        "status": db_campaign.status,
        "prompt": db_campaign.prompt,
        "created_at": db_campaign.created_at,
        "target_industry": db_campaign.target_industry,
        "target_location": db_campaign.target_location,
        "target_employee_count": db_campaign.target_employee_count,
        # Stage-1 input-validation agent output (corrected industry/location + enhanced
        # prompt). The Briefing view renders these validated values, not the raw input.
        "input_validation_review": db_campaign.input_validation_review,
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

    # Serialize exactly as FastAPI would (datetimes/enums via jsonable_encoder), cache
    # the bytes under the current generation, and return them directly.
    body = json.dumps(jsonable_encoder(result)).encode()
    cache_set(_cache_key, body, CAMPAIGN_DETAIL_TTL)
    return Response(content=body, media_type="application/json", headers=_headers)


@router.patch("/{campaign_id}")
def update_campaign(
    campaign_id: str,
    update: CampaignUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Update a campaign's editable fields (human-in-the-loop).
    """
    db_campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not db_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found or access denied")
    
    if update.name:
        db_campaign.name = sanitize_text(update.name, max_length=100)
    if update.prompt:
        db_campaign.prompt = sanitize_text(update.prompt, max_length=2000)
    if update.target_industry:
        db_campaign.target_industry = sanitize_text(update.target_industry, max_length=200)
    if update.target_location:
        db_campaign.target_location = sanitize_text(update.target_location, max_length=200)

    # Single input validator: when any reviewed field changes, re-run the same
    # synchronous review used at creation and persist its corrections. This
    # replaces the old async Track C re-trigger and keeps the val_done gate
    # consistent (the persisted review is the single source of truth).
    if any([update.prompt, update.target_industry, update.target_location]):
        review = input_validation_service.review_inputs(
            target_industry=db_campaign.target_industry or "",
            target_location=db_campaign.target_location or "",
            prompt=db_campaign.prompt,
            sender_website=db_campaign.website,
        )

        # Same offering-question safety net as create_campaign: user_url is mandatory
        # and the offering is sourced from the website by Track B, so an offering-only
        # clarification is redundant and must not block the user.
        _OFFERING_KW = ("offer", "product", "service", "solution", "deliverable", "what do you sell")
        if review.overall.requires_user_clarification:
            real_qs = [
                q for q in (review.overall.clarification_questions or [])
                if not any(k in q.lower() for k in _OFFERING_KW)
            ]
            if not real_qs:
                review.overall.status = "success"
                review.overall.requires_user_clarification = False
                review.overall.clarification_questions = []
                review.prompt.clarification_needed = False
            else:
                review.overall.clarification_questions = real_qs

        db_campaign.input_validation_review = review.model_dump()
        if review.overall.status == "needs_clarification" or review.overall.requires_user_clarification:
            db_campaign.status = models.CampaignStatus.INTERVENTION_NEEDED
        else:
            db_campaign.target_industry = sanitize_text(review.target_industry.corrected, max_length=200) or db_campaign.target_industry
            db_campaign.target_location = sanitize_text(review.target_location.corrected, max_length=200) or db_campaign.target_location
            if review.prompt.enhanced:
                db_campaign.prompt = sanitize_text(review.prompt.enhanced, max_length=2000)
            if db_campaign.status == "INTERVENTION_NEEDED":
                db_campaign.status = models.CampaignStatus.INPUT_VALIDATED

    db.commit()

    return {"message": "Campaign updated successfully"}


# ---------------------------------------------------------------------------
# Dispatch All: bulk-schedule every pending DRAFTED email in a campaign
# ---------------------------------------------------------------------------

@router.post("/{campaign_id}/drafts/dispatch-all")
@limiter.limit("5/minute")
def dispatch_all_drafts(
    request: Request,
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Bulk-schedule all pending DRAFTED emails in a campaign.
    Runs the same timezone-aware delivery-window logic as the individual Deploy button.
    Skips drafts that are already queued, in-progress, or already sent.
    Returns a count summary so the frontend can display results.
    """
    from app.core.scheduler import (
        resolve_prospect_timezone,
        calculate_next_send_slot,
        format_scheduled_display,
    )
    from app.services.draft_dispatch import queue_draft_dispatch

    campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user),
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    # All drafts that are still waiting to be sent
    pending_drafts = (
        db.query(models.EmailDraft)
        .filter(
            models.EmailDraft.campaign_id == campaign_id,
            models.EmailDraft.status == "DRAFTED",
        )
        .all()
    )

    scheduled, skipped, errors = [], [], []

    for draft in pending_drafts:
        draft_id = draft.id
        dm = draft.dm

        # Skip drafts missing an email address — cannot send
        if not dm or not dm.email:
            skipped.append({"draft_id": draft_id, "reason": "no_email"})
            continue

        # Transition to QUEUED (also catches already-sent / in-progress states)
        queued_at = datetime.datetime.now(UTC).replace(tzinfo=None)
        queue_state = queue_draft_dispatch(db, draft, queued_at=queued_at)

        if queue_state == "already_sent":
            skipped.append({"draft_id": draft_id, "dm_name": dm.name, "reason": "already_sent"})
            continue
        if queue_state in ("in_progress", "requires_review"):
            skipped.append({"draft_id": draft_id, "dm_name": dm.name, "reason": queue_state})
            continue
        if queue_state == "queued" and draft.scheduled_at:
            # Already has a slot — count as scheduled and move on
            prospect_tz = dm.display_timezone or "UTC"
            scheduled.append({
                "draft_id": draft_id,
                "dm_name": dm.name,
                "display": format_scheduled_display(draft.scheduled_at, prospect_tz),
                "timezone": prospect_tz,
            })
            continue

        # Resolve timezone + pick next available delivery-window slot
        try:
            prospect_tz = resolve_prospect_timezone(db, dm)
            slot = calculate_next_send_slot(campaign.user_id, prospect_tz, db)
            draft.scheduled_at = slot
            db.commit()
            # Queued with a DB scheduled_at; the durable dispatch poller sends it
            # when due. No fragile per-draft Celery eta (see drafts.py /send).

            scheduled.append({
                "draft_id": draft_id,
                "dm_name": dm.name,
                "display": format_scheduled_display(slot, prospect_tz),
                "timezone": prospect_tz,
            })
            logger.info(
                f"[DISPATCH-ALL] Draft {draft_id} scheduled at {slot} UTC "
                f"(tz: {prospect_tz}) for {dm.name}"
            )
        except Exception as exc:
            db.rollback()
            # Mark the draft as FAILED so it surfaces in the UI
            try:
                draft.dispatch_state = "FAILED"
                draft.dispatch_error = f"Batch dispatch error: {str(exc)}"[:1000]
                db.commit()
            except Exception:
                db.rollback()
            logger.error(
                f"[DISPATCH-ALL] Failed to schedule draft {draft_id}: {exc}",
                exc_info=True,
            )
            errors.append({"draft_id": draft_id, "dm_name": dm.name if dm else "?", "reason": str(exc)[:120]})

    return {
        "scheduled_count": len(scheduled),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "scheduled": scheduled,
        "skipped": skipped,
        "errors": errors,
    }


@router.post("/{campaign_id}/drafts/dispatch-all-now")
@limiter.limit("5/minute")
def dispatch_all_drafts_now(
    request: Request,
    campaign_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Send all pending drafts immediately without scheduling.
    Skips drafts that are already queued, in-progress, or already sent.
    Returns a count summary.
    """
    from app.services.draft_dispatch import queue_draft_dispatch

    campaign = db.query(models.Campaign).filter(
        models.Campaign.id == campaign_id,
        get_visibility_filter(db, current_user),
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    # All drafts that are still waiting to be sent
    pending_drafts = (
        db.query(models.EmailDraft)
        .filter(
            models.EmailDraft.campaign_id == campaign_id,
            models.EmailDraft.status == "DRAFTED",
        )
        .all()
    )

    sent, skipped, errors = [], [], []

    for draft in pending_drafts:
        draft_id = draft.id
        dm = draft.dm

        # Skip drafts missing an email address — cannot send
        if not dm or not dm.email:
            skipped.append({"draft_id": draft_id, "reason": "no_email"})
            continue

        # Transition to QUEUED
        queued_at = datetime.datetime.now(UTC).replace(tzinfo=None)
        queue_state = queue_draft_dispatch(db, draft, queued_at=queued_at)

        if queue_state == "already_sent":
            skipped.append({"draft_id": draft_id, "dm_name": dm.name, "reason": "already_sent"})
            continue
        if queue_state in ("in_progress", "requires_review"):
            skipped.append({"draft_id": draft_id, "dm_name": dm.name, "reason": queue_state})
            continue

        # Send immediately: set scheduled_at to now
        try:
            now_utc = datetime.datetime.now(UTC).replace(tzinfo=None)
            draft.scheduled_at = now_utc
            db.commit()

            sent.append({
                "draft_id": draft_id,
                "dm_name": dm.name,
                "recipient": dm.email,
            })
            logger.info(f"[DISPATCH-ALL-NOW] Draft {draft_id} queued for immediate send")
        except Exception as exc:
            db.rollback()
            try:
                draft.dispatch_state = "FAILED"
                draft.dispatch_error = f"Batch dispatch error: {str(exc)}"[:1000]
                db.commit()
            except Exception:
                db.rollback()
            logger.error(
                f"[DISPATCH-ALL-NOW] Failed to queue draft {draft_id}: {exc}",
                exc_info=True,
            )
            errors.append({"draft_id": draft_id, "dm_name": dm.name if dm else "?", "reason": str(exc)[:120]})

    return {
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "sent": sent,
        "skipped": skipped,
        "errors": errors,
    }
