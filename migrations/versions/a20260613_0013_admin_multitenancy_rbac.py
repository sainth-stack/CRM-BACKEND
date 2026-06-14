"""Admin suite: multi-tenancy, RBAC, and login audit.

Creates tenants, organizations, roles, user_roles and login_sessions; adds
is_active / tenant_id / organization_id to users; and backfills every existing
user into a default tenant + organization so org-scoped queries are non-empty.

Revision ID: a20260613_0013
Revises: a20260612_0012
Create Date: 2026-06-13
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a20260613_0013"
down_revision: Union[str, None] = "a20260612_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    insp = inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # ---- tenants -------------------------------------------------------- #
    if not _has_table("tenants"):
        op.create_table(
            "tenants",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("type", sa.String(), nullable=True),
            sa.Column("timeout", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("name", "type", name="_tenant_name_type_uc"),
        )
        op.create_index("ix_tenants_name", "tenants", ["name"])

    # ---- organizations -------------------------------------------------- #
    if not _has_table("organizations"):
        op.create_table(
            "organizations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("parent_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("logo_url", sa.Text(), nullable=True),
            sa.Column("logo_name", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("name", "tenant_id", "parent_id", name="_org_name_tenant_parent_uc"),
        )
        op.create_index("ix_organizations_name", "organizations", ["name"])
        op.create_index("ix_organizations_tenant_id", "organizations", ["tenant_id"])
        op.create_index("ix_organizations_parent_id", "organizations", ["parent_id"])

    # ---- roles ---------------------------------------------------------- #
    if not _has_table("roles"):
        op.create_table(
            "roles",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("permissions", sa.JSON(), nullable=False),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("name", "organization_id", name="_role_name_org_uc"),
        )
        op.create_index("ix_roles_organization_id", "roles", ["organization_id"])

    # ---- user_roles (assignment join) ----------------------------------- #
    if not _has_table("user_roles"):
        op.create_table(
            "user_roles",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("organization_id", sa.String(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("role_id", sa.String(), sa.ForeignKey("roles.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("user_id", name="_user_single_role_uc"),
        )
        op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
        op.create_index("ix_user_roles_organization_id", "user_roles", ["organization_id"])
        op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])

    # ---- login_sessions (audit) ----------------------------------------- #
    if not _has_table("login_sessions"):
        op.create_table(
            "login_sessions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_email", sa.String(), nullable=True),
            sa.Column("org_id", sa.String(), nullable=True),
            sa.Column("ip_address", sa.String(), nullable=True),
            sa.Column("device_info", sa.Text(), nullable=True),
            sa.Column("login_time", sa.DateTime(), nullable=True),
            sa.Column("logout_time", sa.DateTime(), nullable=True),
            sa.Column("duration_minutes", sa.Float(), nullable=True),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_login_sessions_user_id", "login_sessions", ["user_id"])
        op.create_index("ix_login_sessions_org_id", "login_sessions", ["org_id"])
        op.create_index("ix_login_sessions_status", "login_sessions", ["status"])
        op.create_index("ix_login_sessions_user_status", "login_sessions", ["user_id", "status"])

    # ---- users: new columns --------------------------------------------- #
    if not _has_column("users", "is_active"):
        op.add_column("users", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    if not _has_column("users", "tenant_id"):
        op.add_column("users", sa.Column("tenant_id", sa.String(), nullable=True))
        op.create_foreign_key("fk_users_tenant_id", "users", "tenants", ["tenant_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    if not _has_column("users", "organization_id"):
        op.add_column("users", sa.Column("organization_id", sa.String(), nullable=True))
        op.create_foreign_key("fk_users_organization_id", "users", "organizations", ["organization_id"], ["id"], ondelete="SET NULL")
        op.create_index("ix_users_organization_id", "users", ["organization_id"])

    # ---- backfill: default tenant + organization ------------------------ #
    # Place every pre-existing user into a "Default" tenant/org so org-scoped
    # admin queries return them. Idempotent: only creates rows if absent.
    default_tenant_id = str(uuid.uuid4())
    default_org_id = str(uuid.uuid4())

    existing_tenant = bind.execute(
        sa.text("SELECT id FROM tenants WHERE name = :n"), {"n": "Default Tenant"}
    ).first()
    if existing_tenant:
        default_tenant_id = existing_tenant[0]
    else:
        bind.execute(
            sa.text(
                "INSERT INTO tenants (id, name, type, timeout, created_at) "
                "VALUES (:id, :name, :type, :timeout, :created_at)"
            ),
            {"id": default_tenant_id, "name": "Default Tenant", "type": "internal",
             "timeout": 60, "created_at": sa.func.now()},
        )

    existing_org = bind.execute(
        sa.text("SELECT id FROM organizations WHERE name = :n AND tenant_id = :t"),
        {"n": "Default Organization", "t": default_tenant_id},
    ).first()
    if existing_org:
        default_org_id = existing_org[0]
    else:
        bind.execute(
            sa.text(
                "INSERT INTO organizations (id, name, tenant_id, parent_id, created_at) "
                "VALUES (:id, :name, :tenant_id, NULL, :created_at)"
            ),
            {"id": default_org_id, "name": "Default Organization",
             "tenant_id": default_tenant_id, "created_at": sa.func.now()},
        )

    # Attach only users not already placed (super admins intentionally left
    # tenant-less are out of scope; here we place everyone for a clean default).
    bind.execute(
        sa.text(
            "UPDATE users SET tenant_id = :t, organization_id = :o "
            "WHERE tenant_id IS NULL AND organization_id IS NULL"
        ),
        {"t": default_tenant_id, "o": default_org_id},
    )


def downgrade() -> None:
    # Drop user columns first (FKs reference the new tables).
    for idx, col in (("ix_users_organization_id", "organization_id"),
                     ("ix_users_tenant_id", "tenant_id")):
        if _has_column("users", col):
            try:
                op.drop_index(idx, table_name="users")
            except Exception:
                pass
    if _has_column("users", "organization_id"):
        try:
            op.drop_constraint("fk_users_organization_id", "users", type_="foreignkey")
        except Exception:
            pass
        op.drop_column("users", "organization_id")
    if _has_column("users", "tenant_id"):
        try:
            op.drop_constraint("fk_users_tenant_id", "users", type_="foreignkey")
        except Exception:
            pass
        op.drop_column("users", "tenant_id")
    if _has_column("users", "is_active"):
        op.drop_column("users", "is_active")

    for tbl in ("login_sessions", "user_roles", "roles", "organizations", "tenants"):
        if _has_table(tbl):
            op.drop_table(tbl)
