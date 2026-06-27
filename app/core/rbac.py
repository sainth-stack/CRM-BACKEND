"""
Role-Based Access Control (RBAC) core.

Two layers of authorization:

  1. Coarse *system role* gate — the existing `User.role` enum
     (super_admin / admin / user). Used to decide who may reach the admin
     surface at all (e.g. only super_admin touches tenants).

  2. Fine-grained *permission* gate — custom org-scoped Roles carry a list of
     permission strings (validated against PERMISSION_CATALOG). A user's
     effective permissions come from their UserRoleAssignment -> Role.

`super_admin` implicitly holds every permission.
"""
from typing import List, Callable
from fastapi import Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.db import models
from app.db.database import get_db
from app.core.security import get_current_user


# --------------------------------------------------------------------------- #
# Permission catalog                                                          #
# The single source of truth for assignable permissions. Role creation/updates#
# are validated against this set, and the frontend renders checkboxes from it. #
# --------------------------------------------------------------------------- #
PERMISSION_CATALOG: dict[str, list[str]] = {
    "Campaigns": ["campaign.read", "campaign.write", "campaign.delete"],
    "Drafts": ["draft.read", "draft.write"],
    "Prospects": ["prospect.read"],
    "Users": ["user.read", "user.provision", "user.manage", "user.delete"],
    "Roles": ["role.read", "role.manage"],
    "Organizations": ["org.read", "org.manage"],
    "Tenants": ["tenant.read", "tenant.manage"],
    "Sessions": ["session.read", "session.revoke"],
}

ALL_PERMISSIONS: set[str] = {p for group in PERMISSION_CATALOG.values() for p in group}


def is_super_admin(user: models.User) -> bool:
    return user.role == models.UserRole.SUPER_ADMIN


def is_admin(user: models.User) -> bool:
    return user.role in (models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN)


def get_user_permissions(db: Session, user: models.User) -> List[str]:
    """
    Resolve a user's effective permission strings.
    Super admins implicitly hold every permission. Everyone else inherits the
    permission list of the Role attached to their UserRoleAssignment (if any).
    """
    if is_super_admin(user):
        return sorted(ALL_PERMISSIONS)

    assignment = (
        db.query(models.UserRoleAssignment)
        .filter(models.UserRoleAssignment.user_id == user.id)
        .first()
    )
    if not assignment or not assignment.role_id:
        return []
    role = db.query(models.Role).filter(models.Role.id == assignment.role_id).first()
    if not role or not role.permissions:
        return []
    # Stored as JSON list; defensively coerce.
    return [p for p in role.permissions if isinstance(p, str)]


def validate_permissions(permissions: list[str]) -> list[str]:
    """Reject any permission string not present in the catalog; de-duplicates."""
    cleaned = []
    for p in permissions:
        if p not in ALL_PERMISSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown permission '{p}'. See GET /admin/permissions for valid values.",
            )
        if p not in cleaned:
            cleaned.append(p)
    return cleaned


# --------------------------------------------------------------------------- #
# FastAPI dependencies                                                        #
# --------------------------------------------------------------------------- #
def require_system_role(*allowed: "models.UserRole") -> Callable:
    """
    Dependency factory: 403 unless the caller's system role is in `allowed`.
    Usage:  current_user = Depends(require_system_role(models.UserRole.SUPER_ADMIN))
    """
    def _guard(current_user: models.User = Depends(get_current_user)) -> models.User:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient system authority for this operation.",
            )
        return current_user
    return _guard


def require_permission(permission: str) -> Callable:
    """
    Dependency factory: 403 unless the caller holds `permission` (super admins
    always pass). Returns the resolved user.
    """
    def _guard(
        current_user: models.User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> models.User:
        if is_super_admin(current_user):
            return current_user
        perms = get_user_permissions(db, current_user)
        if permission not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required permission: {permission}",
            )
        return current_user
    return _guard


# --------------------------------------------------------------------------- #
# Pagination helper                                                           #
# Uniform {items, pagination} envelope shared by every admin list endpoint.   #
# --------------------------------------------------------------------------- #
class PageParams:
    """Common query params for paginated + sortable list endpoints."""
    def __init__(
        self,
        page: int = Query(1, ge=1, description="1-based page number"),
        page_size: int = Query(20, ge=1, le=100, description="Results per page"),
        sort: str = Query("created_at", description="Column to sort by"),
        direction: str = Query("desc", pattern="^(asc|desc)$", description="asc|desc"),
    ):
        self.page = page
        self.page_size = page_size
        self.sort = sort
        self.direction = direction


def paginate(query, params: PageParams, model, default_sort_col: str = "created_at"):
    """
    Apply ordering + offset/limit to a SQLAlchemy query and return
    (rows, pagination_dict). Falls back to `default_sort_col` if the requested
    sort column does not exist on the model (prevents injection / attribute err).
    """
    sort_col = getattr(model, params.sort, None)
    if sort_col is None:
        sort_col = getattr(model, default_sort_col)
    sort_col = sort_col.desc() if params.direction == "desc" else sort_col.asc()

    total = query.count()
    offset = (params.page - 1) * params.page_size
    rows = query.order_by(sort_col).offset(offset).limit(params.page_size).all()

    total_pages = (total + params.page_size - 1) // params.page_size if total > 0 else 0
    pagination = {
        "page": params.page,
        "page_size": params.page_size,
        "total_pages": total_pages,
        "total_records": total,
    }
    return rows, pagination
