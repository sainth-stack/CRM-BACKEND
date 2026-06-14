import uuid
import datetime
from datetime import UTC
from sqlalchemy import (
    Column, String, Text, DateTime, ForeignKey, Integer, Float, JSON,
    UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from .base import Base


class Tenant(Base):
    """
    Top-level multi-tenancy container. One tenant per customer/company.
    Everything the customer owns (organizations -> users) lives inside it.
    """
    __tablename__ = "tenants"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, index=True)
    type = Column(String, nullable=True)            # e.g. enterprise / smb / internal
    timeout = Column(Integer, nullable=True)        # session timeout in minutes (mirrors reference)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    organizations = relationship(
        "Organization", back_populates="tenant",
        cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("name", "type", name="_tenant_name_type_uc"),
    )


class Organization(Base):
    """
    A group inside a tenant. Self-referential `parent_id` allows nesting
    (e.g. "Acme" -> "Acme - Sales"). Logo is stored as a URL or base64 data URI
    in `logo_url` (no external object store is wired into this app).
    """
    __tablename__ = "organizations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, index=True)
    tenant_id = Column(String, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_id = Column(String, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    logo_url = Column(Text, nullable=True)
    logo_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    tenant = relationship("Tenant", back_populates="organizations")
    parent = relationship("Organization", remote_side=[id], backref="children")
    roles = relationship(
        "Role", back_populates="organization",
        cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("name", "tenant_id", "parent_id", name="_org_name_tenant_parent_uc"),
    )


class Role(Base):
    """
    A custom, organization-scoped RBAC role carrying a list of permission
    strings (validated against app.core.rbac.PERMISSION_CATALOG).
    """
    __tablename__ = "roles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    permissions = Column(JSON, nullable=False, default=list)
    organization_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    organization = relationship("Organization", back_populates="roles")
    user_roles = relationship("UserRoleAssignment", back_populates="role", passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("name", "organization_id", name="_role_name_org_uc"),
    )


class UserRoleAssignment(Base):
    """
    Assignment join: grants a user a custom Role within an Organization.
    A user has at most one active assignment row (their effective capabilities).

    Named `UserRoleAssignment` (not `UserRole`) to avoid colliding with the
    system-level `UserRole` *enum* in models/user.py, which existing code
    references as `models.UserRole.SUPER_ADMIN`.
    """
    __tablename__ = "user_roles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id = Column(String, ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    user = relationship("User", backref="user_role_assignments")
    organization = relationship("Organization")
    role = relationship("Role", back_populates="user_roles")

    __table_args__ = (
        UniqueConstraint("user_id", name="_user_single_role_uc"),
    )


class LoginSession(Base):
    """
    Login audit trail. One row is written per successful login, capturing the
    client IP and device, and is closed out (logout_time + duration) on logout
    or by the session sweeper.
    """
    __tablename__ = "login_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user_email = Column(String, nullable=True)
    org_id = Column(String, nullable=True, index=True)
    ip_address = Column(String, nullable=True)
    device_info = Column(Text, nullable=True)
    login_time = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    logout_time = Column(DateTime, nullable=True)
    duration_minutes = Column(Float, nullable=True)
    status = Column(String, default="active", index=True)   # active / ended / expired
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    user = relationship("User")

    __table_args__ = (
        Index("ix_login_sessions_user_status", "user_id", "status"),
    )
