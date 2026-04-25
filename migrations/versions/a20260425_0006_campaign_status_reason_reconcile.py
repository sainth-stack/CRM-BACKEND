"""campaign status_reason reconcile

Revision ID: a20260425_0006
Revises: a20260424_0005
Create Date: 2026-04-25 15:40:00
"""

from alembic import op
import sqlalchemy as sa


revision = "a20260425_0006"
down_revision = "a20260424_0005"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("campaigns") and not _has_column(inspector, "campaigns", "status_reason"):
        op.add_column("campaigns", sa.Column("status_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("campaigns") and _has_column(inspector, "campaigns", "status_reason"):
        op.drop_column("campaigns", "status_reason")
