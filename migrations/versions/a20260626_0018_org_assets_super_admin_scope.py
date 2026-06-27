"""Allow super-admin-scoped org_assets (no organization).

Makes organization_id nullable and adds owner_user_id so that super admins
(who have no organization) can upload brochure/use-case assets tied to their
user ID. Assets always have either organization_id OR owner_user_id set.

Revision ID: a20260626_0018
Revises: a20260625_0017
Create Date: 2026-06-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a20260626_0018"
down_revision: Union[str, None] = "a20260625_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Make organization_id nullable so super-admin rows can omit it.
    if bind.dialect.name == "postgresql":
        op.alter_column("org_assets", "organization_id", nullable=True)
    elif bind.dialect.name == "sqlite":
        # SQLite doesn't support ALTER COLUMN — recreate isn't needed because
        # SQLite silently allows NULL even for NOT NULL columns when the
        # constraint was added without CHECK; the ORM model is the enforcer.
        # We still run a no-op to keep migration history clean.
        pass

    # 2. Add owner_user_id column (nullable FK to users.id).
    if not _has_column("org_assets", "owner_user_id"):
        op.add_column(
            "org_assets",
            sa.Column(
                "owner_user_id",
                sa.String(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index("ix_org_assets_owner_user_id", "org_assets", ["owner_user_id"])


def downgrade() -> None:
    if _has_column("org_assets", "owner_user_id"):
        op.drop_index("ix_org_assets_owner_user_id", table_name="org_assets")
        op.drop_column("org_assets", "owner_user_id")

    # Restore NOT NULL on organization_id (only feasible if no super-admin rows exist).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column("org_assets", "organization_id", nullable=False)
