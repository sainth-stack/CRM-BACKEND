"""
Administration API.

Multi-tenant + RBAC + login-audit admin surface, mounted at /admin.

Authority model:
  * Super Admin  — global. Owns Tenants and Organizations everywhere.
  * Admin        — delegated. Manages Roles, role-assignments and Users
                   *within their own tenant* (current_user.tenant_id).
  * User         — no admin access.

Every mutating endpoint records an AdministrativeLog row and, where it changes
a user's access, revokes that user's live sessions.
"""
from typing import Optional
from datetime import datetime, UTC
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import models
from app.db.database import get_db
from app.core.security import get_current_user, revoke_sessions
from app.core.logging_config import logger
from app.core.rbac import (
    PERMISSION_CATALOG,
    get_user_permissions,
    validate_permissions,
    is_super_admin,
    require_system_role,
    require_permission,
    PageParams,
    paginate,
)

router = APIRouter(prefix="/admin", tags=["Administration"])

SUPER = models.UserRole.SUPER_ADMIN
ADMIN = models.UserRole.ADMIN


# --------------------------------------------------------------------------- #
# Shared helpers                                                              #
# --------------------------------------------------------------------------- #
def _audit(db: Session, actor_id: str, action: str, target_id: str | None, details: str):
    """Append an AdministrativeLog row (caller is responsible for the commit)."""
    db.add(models.AdministrativeLog(
        actor_id=actor_id, target_id=target_id, action=action, details=details
    ))


def _require_tenant_scope(current_user: models.User) -> Optional[str]:
    """
    Returns the tenant a non-super-admin is confined to. Super admins return
    None (no confinement). Admins without a tenant cannot manage anything.
    """
    if is_super_admin(current_user):
        return None
    if not current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your identity is not attached to a tenant. Contact a Super Admin.",
        )
    return current_user.tenant_id


def _get_org_or_404(db: Session, org_id: str) -> models.Organization:
    org = db.query(models.Organization).filter(models.Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return org


def _assert_org_in_scope(current_user: models.User, org: models.Organization):
    """Admins may only operate on organizations inside their own tenant."""
    tenant_scope = _require_tenant_scope(current_user)
    if tenant_scope is not None and org.tenant_id != tenant_scope:
        raise HTTPException(status_code=403, detail="Organization is outside your tenant.")


def _tenant_brief(t: models.Tenant | None) -> dict | None:
    if not t:
        return None
    return {"tenant_id": t.id, "tenant_name": t.name, "tenant_type": t.type, "tenant_timeout": t.timeout}


def _org_brief(o: models.Organization | None) -> dict | None:
    if not o:
        return None
    return {"organization_id": o.id, "organization_name": o.name,
            "parent_id": o.parent_id, "logo_url": o.logo_url, "logo_name": o.logo_name}


# =========================================================================== #
# PERMISSION CATALOG                                                          #
# =========================================================================== #
@router.get("/permissions")
def list_permissions(current_user: models.User = Depends(require_system_role(SUPER, ADMIN))):
    """Returns the catalog of assignable permission strings (feeds role UI)."""
    return {"permissions": PERMISSION_CATALOG}


# =========================================================================== #
# TENANTS  (Super Admin only)                                                 #
# =========================================================================== #
class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    type: Optional[str] = Field(None, max_length=60)
    timeout: Optional[int] = Field(None, ge=0)


class TenantUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=160)
    type: Optional[str] = Field(None, max_length=60)
    timeout: Optional[int] = Field(None, ge=0)


def _tenant_detail(t: models.Tenant) -> dict:
    return {"id": t.id, "name": t.name, "type": t.type, "timeout": t.timeout, "created_at": t.created_at}


@router.post("/tenants", status_code=201)
def create_tenant(
    payload: TenantCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    dup = db.query(models.Tenant).filter(
        models.Tenant.name == payload.name, models.Tenant.type == payload.type
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="A tenant with this name and type already exists.")

    tenant = models.Tenant(name=payload.name, type=payload.type, timeout=payload.timeout)
    db.add(tenant)
    db.flush()
    _audit(db, current_user.id, "TENANT_CREATE", tenant.id, f"Created tenant {tenant.name}")
    db.commit()
    return _tenant_detail(tenant)


@router.get("/tenants")
def list_tenants(
    params: PageParams = Depends(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    rows, pagination = paginate(db.query(models.Tenant), params, models.Tenant)
    return {"tenants": [_tenant_detail(t) for t in rows], "pagination": pagination}


@router.get("/tenants/{tenant_id}")
def get_tenant(
    tenant_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return _tenant_detail(tenant)


@router.patch("/tenants/{tenant_id}")
def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(tenant, field, value)
    _audit(db, current_user.id, "TENANT_UPDATE", tenant.id, f"Updated tenant fields: {list(data)}")
    db.commit()
    return _tenant_detail(tenant)


@router.delete("/tenants/{tenant_id}")
def delete_tenant(
    tenant_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    org_count = db.query(models.Organization).filter(models.Organization.tenant_id == tenant_id).count()
    if org_count:
        raise HTTPException(
            status_code=409,
            detail=f"Tenant still contains {org_count} organization(s). Remove them first.",
        )
    _audit(db, current_user.id, "TENANT_DELETE", tenant.id, f"Deleted tenant {tenant.name}")
    db.delete(tenant)
    db.commit()
    return {"message": "Tenant deleted successfully."}


# =========================================================================== #
# ORGANIZATIONS                                                               #
# =========================================================================== #
class OrgCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    tenant_id: str
    parent_id: Optional[str] = None
    logo_url: Optional[str] = None   # URL or base64 data URI
    logo_name: Optional[str] = None


class OrgUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=160)
    parent_id: Optional[str] = None
    logo_url: Optional[str] = None
    logo_name: Optional[str] = None


def _org_detail(db: Session, o: models.Organization) -> dict:
    tenant = db.query(models.Tenant).filter(models.Tenant.id == o.tenant_id).first()
    return {
        "id": o.id, "name": o.name, "tenant": _tenant_brief(tenant),
        "parent_id": o.parent_id, "logo_url": o.logo_url, "logo_name": o.logo_name,
        "created_at": o.created_at,
    }


@router.post("/organizations", status_code=201)
def create_organization(
    payload: OrgCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == payload.tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=400, detail="Referenced tenant does not exist.")

    if payload.parent_id:
        parent = _get_org_or_404(db, payload.parent_id)
        if parent.tenant_id != payload.tenant_id:
            raise HTTPException(status_code=400, detail="Parent organization belongs to a different tenant.")

    dup = db.query(models.Organization).filter(
        models.Organization.name == payload.name,
        models.Organization.tenant_id == payload.tenant_id,
        models.Organization.parent_id == payload.parent_id,
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="Organization with these details already exists.")

    org = models.Organization(
        name=payload.name, tenant_id=payload.tenant_id, parent_id=payload.parent_id,
        logo_url=payload.logo_url, logo_name=payload.logo_name,
    )
    db.add(org)
    db.flush()
    _audit(db, current_user.id, "ORG_CREATE", org.id, f"Created organization {org.name}")
    db.commit()
    return _org_detail(db, org)


@router.get("/organizations")
def list_organizations(
    tenant_id: Optional[str] = Query(None),
    params: PageParams = Depends(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    query = db.query(models.Organization)
    scope = _require_tenant_scope(current_user)
    if scope is not None:
        query = query.filter(models.Organization.tenant_id == scope)
    elif tenant_id:
        query = query.filter(models.Organization.tenant_id == tenant_id)

    rows, pagination = paginate(query, params, models.Organization)
    return {"organizations": [_org_detail(db, o) for o in rows], "pagination": pagination}


@router.get("/organizations/{org_id}")
def get_organization(
    org_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    org = _get_org_or_404(db, org_id)
    _assert_org_in_scope(current_user, org)
    return _org_detail(db, org)


@router.patch("/organizations/{org_id}")
def update_organization(
    org_id: str,
    payload: OrgUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    org = _get_org_or_404(db, org_id)
    data = payload.model_dump(exclude_unset=True)

    if "parent_id" in data and data["parent_id"]:
        if data["parent_id"] == org_id:
            raise HTTPException(status_code=400, detail="An organization cannot be its own parent.")
        parent = _get_org_or_404(db, data["parent_id"])
        if parent.tenant_id != org.tenant_id:
            raise HTTPException(status_code=400, detail="Parent organization belongs to a different tenant.")

    for field, value in data.items():
        setattr(org, field, value)
    _audit(db, current_user.id, "ORG_UPDATE", org.id, f"Updated organization fields: {list(data)}")
    db.commit()
    return _org_detail(db, org)


@router.delete("/organizations/{org_id}")
def delete_organization(
    org_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER)),
):
    org = _get_org_or_404(db, org_id)

    child_count = db.query(models.Organization).filter(models.Organization.parent_id == org_id).count()
    member_count = db.query(models.User).filter(models.User.organization_id == org_id).count()
    if child_count or member_count:
        raise HTTPException(
            status_code=409,
            detail=f"Organization has {child_count} child org(s) and {member_count} member(s). Reassign or remove them first.",
        )
    _audit(db, current_user.id, "ORG_DELETE", org.id, f"Deleted organization {org.name}")
    db.delete(org)
    db.commit()
    return {"message": "Organization deleted successfully."}


# =========================================================================== #
# ROLES (RBAC)                                                                #
# =========================================================================== #
class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    permissions: list[str] = Field(default_factory=list)
    organization_id: str


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    permissions: Optional[list[str]] = None


def _role_detail(r: models.Role) -> dict:
    return {"id": r.id, "name": r.name, "permissions": r.permissions,
            "organization_id": r.organization_id, "created_at": r.created_at}


@router.post("/roles", status_code=201)
def create_role(
    payload: RoleCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    org = _get_org_or_404(db, payload.organization_id)
    _assert_org_in_scope(current_user, org)
    perms = validate_permissions(payload.permissions)

    dup = db.query(models.Role).filter(
        models.Role.name == payload.name,
        models.Role.organization_id == payload.organization_id,
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="A role with this name already exists in the organization.")

    role = models.Role(name=payload.name, permissions=perms, organization_id=payload.organization_id)
    db.add(role)
    db.flush()
    _audit(db, current_user.id, "ROLE_CREATE", role.id, f"Created role {role.name} ({perms})")
    db.commit()
    return _role_detail(role)


@router.get("/roles")
def list_roles(
    organization_id: Optional[str] = Query(None),
    params: PageParams = Depends(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    query = db.query(models.Role)
    scope = _require_tenant_scope(current_user)
    if scope is not None:
        # Confine to roles whose org is in the admin's tenant.
        tenant_org_ids = db.query(models.Organization.id).filter(models.Organization.tenant_id == scope)
        query = query.filter(models.Role.organization_id.in_(tenant_org_ids))
    if organization_id:
        query = query.filter(models.Role.organization_id == organization_id)

    rows, pagination = paginate(query, params, models.Role)
    return {"roles": [_role_detail(r) for r in rows], "pagination": pagination}


@router.get("/roles/{role_id}")
def get_role(
    role_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    role = db.query(models.Role).filter(models.Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found.")
    _assert_org_in_scope(current_user, _get_org_or_404(db, role.organization_id))
    return _role_detail(role)


@router.patch("/roles/{role_id}")
def update_role(
    role_id: str,
    payload: RoleUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    role = db.query(models.Role).filter(models.Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found.")
    _assert_org_in_scope(current_user, _get_org_or_404(db, role.organization_id))

    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        role.name = data["name"]
    if "permissions" in data and data["permissions"] is not None:
        role.permissions = validate_permissions(data["permissions"])

    _audit(db, current_user.id, "ROLE_UPDATE", role.id, f"Updated role fields: {list(data)}")
    db.commit()

    # Permission changes take effect on next request; revoke sessions of holders
    # so they cannot keep acting on a stale permission set.
    if "permissions" in data:
        holders = db.query(models.UserRoleAssignment).filter(
            models.UserRoleAssignment.role_id == role.id
        ).all()
        for h in holders:
            revoke_sessions(h.user_id)

    return _role_detail(role)


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    role = db.query(models.Role).filter(models.Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found.")
    _assert_org_in_scope(current_user, _get_org_or_404(db, role.organization_id))

    assigned = db.query(models.UserRoleAssignment).filter(
        models.UserRoleAssignment.role_id == role_id
    ).count()
    if assigned:
        raise HTTPException(
            status_code=409,
            detail=f"Role is assigned to {assigned} user(s). Reassign them before deleting.",
        )
    _audit(db, current_user.id, "ROLE_DELETE", role.id, f"Deleted role {role.name}")
    db.delete(role)
    db.commit()
    return {"message": "Role deleted successfully."}


# =========================================================================== #
# ROLE ASSIGNMENT                                                             #
# =========================================================================== #
class AssignRoleRequest(BaseModel):
    user_id: str
    organization_id: str
    role_id: Optional[str] = None


@router.post("/user-roles")
def assign_user_role(
    payload: AssignRoleRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    """Assign (or replace) a user's role within an organization."""
    target = db.query(models.User).filter(models.User.id == payload.user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target user not found.")

    org = _get_org_or_404(db, payload.organization_id)
    _assert_org_in_scope(current_user, org)

    role = None
    if payload.role_id:
        role = db.query(models.Role).filter(models.Role.id == payload.role_id).first()
        if not role:
            raise HTTPException(status_code=400, detail="Referenced role does not exist.")
        if role.organization_id != org.id:
            raise HTTPException(status_code=400, detail="Role does not belong to the target organization.")

    # One assignment per user: upsert.
    assignment = db.query(models.UserRoleAssignment).filter(
        models.UserRoleAssignment.user_id == payload.user_id
    ).first()
    if assignment:
        assignment.organization_id = org.id
        assignment.role_id = payload.role_id
    else:
        assignment = models.UserRoleAssignment(
            user_id=payload.user_id, organization_id=org.id, role_id=payload.role_id
        )
        db.add(assignment)

    # Keep the denormalized placement on the user in sync.
    target.organization_id = org.id
    target.tenant_id = org.tenant_id

    _audit(db, current_user.id, "ROLE_ASSIGN", target.id,
           f"Assigned role={payload.role_id} org={org.id} to {target.email}")
    db.commit()
    revoke_sessions(target.id)  # force re-login so new permissions/placement apply

    return {
        "message": "Role assigned successfully.",
        "assignment": {
            "user_id": target.id, "organization_id": org.id, "role_id": payload.role_id,
        },
    }


# =========================================================================== #
# USERS                                                                       #
# =========================================================================== #
class UserAdminUpdate(BaseModel):
    email: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None
    organization_id: Optional[str] = None
    system_role: Optional[str] = None   # super_admin / admin / user


def _user_detail(db: Session, u: models.User) -> dict:
    org = db.query(models.Organization).filter(models.Organization.id == u.organization_id).first() if u.organization_id else None
    tenant = db.query(models.Tenant).filter(models.Tenant.id == u.tenant_id).first() if u.tenant_id else None
    assignment = db.query(models.UserRoleAssignment).filter(models.UserRoleAssignment.user_id == u.id).first()
    custom_role = db.query(models.Role).filter(models.Role.id == assignment.role_id).first() if (assignment and assignment.role_id) else None
    return {
        "id": u.id,
        "email": u.email,
        "system_role": u.role.value,
        "is_active": u.is_active,
        "is_demo": u.is_demo,
        "is_activated": u.hashed_password is not None,
        "tenant": _tenant_brief(tenant),
        "organization": _org_brief(org),
        "custom_role": {"id": custom_role.id, "name": custom_role.name,
                        "permissions": custom_role.permissions} if custom_role else None,
        "permissions": get_user_permissions(db, u),
        "created_at": u.created_at,
    }


@router.get("/users")
def list_users(
    organization_id: Optional[str] = Query(None),
    system_role: Optional[str] = Query(None),
    params: PageParams = Depends(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    query = db.query(models.User)
    scope = _require_tenant_scope(current_user)
    if scope is not None:
        query = query.filter(models.User.tenant_id == scope)
    if organization_id:
        query = query.filter(models.User.organization_id == organization_id)
    if system_role:
        try:
            query = query.filter(models.User.role == models.UserRole(system_role))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown system role '{system_role}'.")

    rows, pagination = paginate(query, params, models.User)
    return {"users": [_user_detail(db, u) for u in rows], "pagination": pagination}


@router.get("/users/{user_id}")
def get_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    scope = _require_tenant_scope(current_user)
    if scope is not None and user.tenant_id != scope:
        raise HTTPException(status_code=403, detail="User is outside your tenant.")
    return _user_detail(db, user)


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserAdminUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    scope = _require_tenant_scope(current_user)
    if scope is not None and user.tenant_id != scope:
        raise HTTPException(status_code=403, detail="User is outside your tenant.")

    data = payload.model_dump(exclude_unset=True)
    access_changed = False

    if "email" in data and data["email"]:
        clash = db.query(models.User).filter(
            models.User.email == data["email"], models.User.id != user.id
        ).first()
        if clash:
            raise HTTPException(status_code=400, detail="Another identity already uses this email.")
        user.email = data["email"]

    if "is_active" in data and data["is_active"] is not None:
        user.is_active = data["is_active"]
        if not data["is_active"]:
            access_changed = True

    if "organization_id" in data and data["organization_id"]:
        org = _get_org_or_404(db, data["organization_id"])
        _assert_org_in_scope(current_user, org)
        user.organization_id = org.id
        user.tenant_id = org.tenant_id

    if "system_role" in data and data["system_role"]:
        # Only a Super Admin may change system roles (and thus mint admins).
        if not is_super_admin(current_user):
            raise HTTPException(status_code=403, detail="Only a Super Admin may change system roles.")
        try:
            user.role = models.UserRole(data["system_role"])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown system role '{data['system_role']}'.")
        access_changed = True

    _audit(db, current_user.id, "USER_UPDATE", user.id, f"Updated user fields: {list(data)}")
    db.commit()

    if access_changed:
        revoke_sessions(user.id)

    return _user_detail(db, user)


# =========================================================================== #
# LOGIN SESSIONS (audit)                                                      #
# =========================================================================== #
def _session_detail(db: Session, s: models.LoginSession) -> dict:
    return {
        "id": s.id, "user_id": s.user_id, "user_email": s.user_email, "org_id": s.org_id,
        "ip_address": s.ip_address, "device_info": s.device_info,
        "login_time": s.login_time, "logout_time": s.logout_time,
        "duration_minutes": s.duration_minutes, "status": s.status,
    }


@router.get("/sessions")
def list_sessions(
    org_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    params: PageParams = Depends(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    query = db.query(models.LoginSession)

    # Tenant confinement for admins: only sessions of users in their tenant.
    scope = _require_tenant_scope(current_user)
    if scope is not None:
        tenant_user_ids = db.query(models.User.id).filter(models.User.tenant_id == scope)
        query = query.filter(models.LoginSession.user_id.in_(tenant_user_ids))

    if org_id:
        query = query.filter(models.LoginSession.org_id == org_id)
    if user_id:
        query = query.filter(models.LoginSession.user_id == user_id)
    if start_date:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.filter(models.LoginSession.login_time >= start)
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD.")
    if end_date:
        try:
            end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            query = query.filter(models.LoginSession.login_time <= end)
        except ValueError:
            raise HTTPException(status_code=400, detail="end_date must be YYYY-MM-DD.")

    active_count = query.filter(models.LoginSession.status == "active").count()
    rows, pagination = paginate(query, params, models.LoginSession, default_sort_col="login_time")
    return {
        "sessions": [_session_detail(db, s) for s in rows],
        "active_sessions": active_count,
        "pagination": pagination,
    }


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    s = db.query(models.LoginSession).filter(models.LoginSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")
    scope = _require_tenant_scope(current_user)
    if scope is not None:
        owner = db.query(models.User).filter(models.User.id == s.user_id).first()
        if not owner or owner.tenant_id != scope:
            raise HTTPException(status_code=403, detail="Session is outside your tenant.")
    return _session_detail(db, s)


@router.post("/sessions/{session_id}/revoke")
def revoke_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_system_role(SUPER, ADMIN)),
):
    """Force-logout: closes the audit row and revokes the user's live tokens."""
    s = db.query(models.LoginSession).filter(models.LoginSession.id == session_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")

    scope = _require_tenant_scope(current_user)
    if scope is not None:
        owner = db.query(models.User).filter(models.User.id == s.user_id).first()
        if not owner or owner.tenant_id != scope:
            raise HTTPException(status_code=403, detail="Session is outside your tenant.")

    now = datetime.now(UTC)
    if s.status == "active":
        s.logout_time = now
        if s.login_time:
            login_aware = s.login_time if s.login_time.tzinfo else s.login_time.replace(tzinfo=UTC)
            s.duration_minutes = round((now - login_aware).total_seconds() / 60.0, 2)
        s.status = "ended"

    _audit(db, current_user.id, "SESSION_REVOKE", s.user_id, f"Force-revoked session {s.id}")
    db.commit()
    revoke_sessions(s.user_id)
    return {"message": "Session revoked and user sessions terminated."}
