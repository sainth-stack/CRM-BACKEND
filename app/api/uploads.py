"""
File upload endpoints — mounted at /uploads.

Admins upload PDFs (brochures / use-case docs) for their organization.
All users in the same organization can list and download those assets.

The download link returned to the frontend is a permanent backend URL:
    GET /uploads/assets/{id}   (no auth required)
which generates a fresh presigned S3 URL on every request — safe to
embed as a hyperlink in outreach emails because the backend URL never
expires; only the underlying presigned URL rotates on each click.
"""
import uuid
import logging
import datetime
from typing import Optional

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import get_current_user
from app.core.rbac import require_system_role, is_super_admin
from app.db.database import get_db
from app.db import models

logger = logging.getLogger("uploads")
router = APIRouter()

MAX_PDF_SIZE_BYTES = 20 * 1024 * 1024   # 20 MB
PRESIGN_TTL = 7 * 24 * 3600             # 7 days (S3 max for IAM-signed URLs)

ADMIN = models.UserRole.ADMIN
SUPER = models.UserRole.SUPER_ADMIN


class AssetReorderRequest(BaseModel):
    asset_type: str
    ordered_ids: list[str]


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _get_s3():
    # Lazy import: boto3 costs noticeable startup time/RAM and is only needed
    # by this file's upload/download endpoints, not the API's hot paths.
    import boto3
    kwargs = {"region_name": settings.AWS_REGION}
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    return boto3.client("s3", **kwargs)


def _resolve_owner_scope(user: models.User) -> tuple[str | None, str | None]:
    """
    Returns (org_id, owner_user_id) depending on the caller's role.

    * Regular admin  → (user.organization_id, None)  — scoped to their org
    * Super admin    → (None, user.id)               — scoped to their user ID

    Raises 403 if a regular admin has no org attached.
    """
    if is_super_admin(user):
        return (None, user.id)
    if not user.organization_id:
        raise HTTPException(
            status_code=403,
            detail="Your account is not attached to an organization.",
        )
    return (user.organization_id, None)


def _asset_query_filter(db, org_id: str | None, owner_user_id: str | None):
    """Return a SQLAlchemy query on OrgAsset filtered to the right scope."""
    q = db.query(models.OrgAsset)
    if org_id:
        return q.filter(models.OrgAsset.organization_id == org_id)
    return q.filter(models.OrgAsset.owner_user_id == owner_user_id)


def _asset_dict(asset: models.OrgAsset) -> dict:
    return {
        "id": asset.id,
        "asset_type": asset.asset_type,
        "filename": asset.filename,
        "file_size": asset.file_size,
        "display_order": asset.display_order,
        "created_at": asset.created_at.isoformat() if asset.created_at else None,
        "uploaded_by": asset.uploaded_by.email if asset.uploaded_by else None,
    }


async def _upload_org_asset(
    asset_type: str,
    file: UploadFile,
    db: Session,
    current_user: models.User,
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are accepted.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(contents) > MAX_PDF_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 20 MB limit.")

    org_id, owner_user_id = _resolve_owner_scope(current_user)
    # Build an S3 key that works for both scopes
    scope_segment = f"org-{org_id}" if org_id else f"user-{owner_user_id}"
    safe_name = file.filename.replace(" ", "_")
    key = settings.s3_key(f"org-assets/{scope_segment}/{asset_type}s/{uuid.uuid4()}_{safe_name}")

    try:
        s3 = _get_s3()
        s3.put_object(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME,
            Key=key,
            Body=contents,
            ContentType="application/pdf",
            ContentDisposition="inline",
        )
    except ClientError as e:
        logger.error(f"S3 upload failed for scope {scope_segment}: {e}")
        raise HTTPException(status_code=502, detail="Failed to upload file to storage.")

    existing_count = _asset_query_filter(db, org_id, owner_user_id).filter(
        models.OrgAsset.asset_type == asset_type
    ).count()

    asset = models.OrgAsset(
        organization_id=org_id,
        owner_user_id=owner_user_id,
        asset_type=asset_type,
        filename=file.filename,
        s3_key=key,
        file_size=len(contents),
        display_order=existing_count,
        uploaded_by_id=current_user.id,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    logger.info(f"{asset_type} uploaded: {key} by user {current_user.id}")
    return _asset_dict(asset)


# --------------------------------------------------------------------------- #
# Org-scoped asset endpoints                                                  #
# --------------------------------------------------------------------------- #

@router.post("/brochure", status_code=201)
async def upload_brochure(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    """Upload a brochure PDF for the admin's organization."""
    return await _upload_org_asset("brochure", file, db, current_user)


@router.post("/usecase", status_code=201)
async def upload_usecase(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    """Upload a use-case PDF for the admin's organization."""
    return await _upload_org_asset("usecase", file, db, current_user)


@router.get("/assets")
def list_assets(
    asset_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    List org assets visible to the caller.
    Super admins see their own user-scoped assets; regular users see their org's assets.
    Optionally filter by asset_type=brochure|usecase.
    """
    org_id, owner_user_id = _resolve_owner_scope(current_user)
    query = _asset_query_filter(db, org_id, owner_user_id)
    if asset_type in ("brochure", "usecase"):
        query = query.filter(models.OrgAsset.asset_type == asset_type)
    assets = query.order_by(models.OrgAsset.display_order.asc(), models.OrgAsset.created_at.asc()).all()
    return {"assets": [_asset_dict(a) for a in assets]}


@router.patch("/assets/reorder")
def reorder_assets(
    payload: AssetReorderRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    """
    Reorder brochure/use-case assets within the caller's scope.
    The first asset has highest priority in reminder selection.
    """
    if payload.asset_type not in ("brochure", "usecase"):
        raise HTTPException(status_code=400, detail="asset_type must be brochure or usecase.")
    if len(payload.ordered_ids) != len(set(payload.ordered_ids)):
        raise HTTPException(status_code=400, detail="ordered_ids contains duplicate asset IDs.")

    org_id, owner_user_id = _resolve_owner_scope(current_user)
    assets = (
        _asset_query_filter(db, org_id, owner_user_id)
        .filter(models.OrgAsset.asset_type == payload.asset_type)
        .all()
    )
    by_id = {asset.id: asset for asset in assets}
    missing = [asset_id for asset_id in payload.ordered_ids if asset_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail="One or more assets were not found in your scope.")

    ordered = [by_id[asset_id] for asset_id in payload.ordered_ids]
    remaining = sorted(
        [asset for asset in assets if asset.id not in set(payload.ordered_ids)],
        key=lambda asset: (asset.display_order, asset.created_at or datetime.datetime.min, asset.id),
    )
    for index, asset in enumerate([*ordered, *remaining]):
        asset.display_order = index
    db.commit()

    refreshed = (
        _asset_query_filter(db, org_id, owner_user_id)
        .filter(models.OrgAsset.asset_type == payload.asset_type)
        .order_by(models.OrgAsset.display_order.asc(), models.OrgAsset.created_at.asc())
        .all()
    )
    return {"assets": [_asset_dict(asset) for asset in refreshed]}


@router.get("/assets/{asset_id}")
def download_asset(
    asset_id: str,
    db: Session = Depends(get_db),
):
    """
    Public endpoint (no auth required) — generates a fresh presigned S3 URL
    and redirects (302). This is the permanent hyperlink embedded in emails:
        {API_BASE_URL}/uploads/assets/{id}
    The backend URL never expires; the presigned URL refreshes on each click.
    """
    asset = db.query(models.OrgAsset).filter(models.OrgAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found.")
    try:
        s3 = _get_s3()
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
                "Key": asset.s3_key,
                "ResponseContentDisposition": "inline",
                "ResponseContentType": "application/pdf",
            },
            ExpiresIn=PRESIGN_TTL,
        )
    except ClientError as e:
        logger.error(f"Presign failed for asset {asset_id}: {e}")
        raise HTTPException(status_code=502, detail="Failed to generate download link.")
    return RedirectResponse(url=presigned_url, status_code=302)


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(
    asset_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    """
    Delete an asset record and the underlying S3 object.
    Admins may only delete assets belonging to their own organization.
    Super admins may only delete their own user-scoped assets.
    """
    asset = db.query(models.OrgAsset).filter(models.OrgAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found.")

    org_id, owner_user_id = _resolve_owner_scope(current_user)
    if org_id and asset.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Asset belongs to a different organization.")
    if owner_user_id and asset.owner_user_id != owner_user_id:
        raise HTTPException(status_code=403, detail="Asset belongs to a different user.")

    try:
        s3 = _get_s3()
        s3.delete_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=asset.s3_key)
    except ClientError as e:
        logger.warning(f"S3 delete failed for {asset.s3_key}: {e}")

    db.delete(asset)
    db.commit()
