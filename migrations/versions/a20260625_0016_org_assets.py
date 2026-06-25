"""Org assets table for brochure and use-case file uploads.

Revision ID: a20260625_0016
Revises: a20260620_0015
Create Date: 2026-06-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a20260625_0016"
down_revision: Union[str, None] = "a20260620_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("org_assets"):
        return

    op.create_table(
        "org_assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_type", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("s3_key", sa.String(), nullable=False, unique=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column(
            "uploaded_by_id",
            sa.String(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_org_assets_organization_id", "org_assets", ["organization_id"])
    op.create_index("ix_org_assets_asset_type", "org_assets", ["asset_type"])


def downgrade() -> None:
    op.drop_table("org_assets")
