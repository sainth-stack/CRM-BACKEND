"""Six-reminder sequence and ordered org assets.

Revision ID: a20260625_0017
Revises: a20260625_0016
Create Date: 2026-06-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision: str = "a20260625_0017"
down_revision: Union[str, None] = "a20260625_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    has_org_assets = _has_table("org_assets")
    if has_org_assets and _has_column("org_assets", "display_order") is False:
        op.add_column(
            "org_assets",
            sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_org_assets_display_order", "org_assets", ["display_order"])
        op.alter_column("org_assets", "display_order", server_default=None)

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS 'REMINDER_3_SENT'")
        op.execute("ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS 'REMINDER_4_SENT'")
        op.execute("ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS 'REMINDER_5_SENT'")
        op.execute("ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS 'REMINDER_6_SENT'")

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY organization_id, asset_type
                           ORDER BY created_at ASC, id ASC
                       ) - 1 AS rn
                FROM org_assets
            )
            UPDATE org_assets
            SET display_order = ranked.rn
            FROM ranked
            WHERE org_assets.id = ranked.id
            """
        )
    elif has_org_assets and bind.dialect.name == "sqlite":
        rows = bind.execute(
            text(
                """
                SELECT id, organization_id, asset_type
                FROM org_assets
                ORDER BY organization_id, asset_type, created_at ASC, id ASC
                """
            )
        ).fetchall()
        counters: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row.organization_id, row.asset_type)
            order = counters.get(key, 0)
            bind.execute(
                text("UPDATE org_assets SET display_order = :order WHERE id = :id"),
                {"order": order, "id": row.id},
            )
            counters[key] = order + 1


def downgrade() -> None:
    if _has_column("org_assets", "display_order"):
        op.drop_index("ix_org_assets_display_order", table_name="org_assets")
        op.drop_column("org_assets", "display_order")
