"""fix legacy campaign columns

Revision ID: a20260425_0007
Revises: a20260425_0006
Create Date: 2026-04-25 15:55:00
"""

from alembic import op
import sqlalchemy as sa


revision = "a20260425_0007"
down_revision = "a20260425_0006"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("campaigns"):
        if not _has_column(inspector, "campaigns", "last_heartbeat"):
            op.add_column("campaigns", sa.Column("last_heartbeat", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "campaigns", "locked_by"):
            op.add_column("campaigns", sa.Column("locked_by", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("campaigns"):
        if _has_column(inspector, "campaigns", "last_heartbeat"):
            op.drop_column("campaigns", "last_heartbeat")
        if _has_column(inspector, "campaigns", "locked_by"):
            op.drop_column("campaigns", "locked_by")
