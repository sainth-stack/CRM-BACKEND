from app.core.config import settings
from app.db import models


def get_asset_download_url(asset_id: str) -> str:
    return f"{settings.API_BASE_URL}/uploads/assets/{asset_id}"


def get_org_brochure(
    db,
    org_id: str | None,
    owner_user_id: str | None = None,
) -> models.OrgAsset | None:
    """
    Fetch the first (highest-priority) brochure for either an org or a super admin user.

    * Regular admin/user: pass org_id, leave owner_user_id=None
    * Super admin (no org): pass org_id=None, owner_user_id=<user.id>
    """
    if org_id:
        return (
            db.query(models.OrgAsset)
            .filter(
                models.OrgAsset.organization_id == org_id,
                models.OrgAsset.asset_type == "brochure",
            )
            .order_by(models.OrgAsset.display_order.asc(), models.OrgAsset.created_at.asc())
            .first()
        )
    if owner_user_id:
        return (
            db.query(models.OrgAsset)
            .filter(
                models.OrgAsset.owner_user_id == owner_user_id,
                models.OrgAsset.asset_type == "brochure",
            )
            .order_by(models.OrgAsset.display_order.asc(), models.OrgAsset.created_at.asc())
            .first()
        )
    return None


def get_org_usecases(
    db,
    org_id: str | None,
    limit: int = 3,
    owner_user_id: str | None = None,
) -> list[models.OrgAsset]:
    """
    Fetch up to `limit` use-case assets for either an org or a super admin user.

    * Regular admin/user: pass org_id, leave owner_user_id=None
    * Super admin (no org): pass org_id=None, owner_user_id=<user.id>
    """
    if org_id:
        return (
            db.query(models.OrgAsset)
            .filter(
                models.OrgAsset.organization_id == org_id,
                models.OrgAsset.asset_type == "usecase",
            )
            .order_by(models.OrgAsset.display_order.asc(), models.OrgAsset.created_at.asc())
            .limit(limit)
            .all()
        )
    if owner_user_id:
        return (
            db.query(models.OrgAsset)
            .filter(
                models.OrgAsset.owner_user_id == owner_user_id,
                models.OrgAsset.asset_type == "usecase",
            )
            .order_by(models.OrgAsset.display_order.asc(), models.OrgAsset.created_at.asc())
            .limit(limit)
            .all()
        )
    return []
