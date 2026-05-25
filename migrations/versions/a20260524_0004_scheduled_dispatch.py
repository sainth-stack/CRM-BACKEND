"""add scheduled_at to email_drafts and outbound_dispatches

Revision ID: a20260524_0004
Revises: c740bed4034c
Create Date: 2026-05-24 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "a20260524_0004"
down_revision = "c740bed4034c"
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(inspector, table: str, index: str) -> bool:
    return any(i["name"] == index for i in inspector.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("email_drafts") and not _has_column(inspector, "email_drafts", "scheduled_at"):
        op.add_column("email_drafts", sa.Column("scheduled_at", sa.DateTime(), nullable=True))
        op.create_index("ix_email_drafts_scheduled_at", "email_drafts", ["scheduled_at"])

    if inspector.has_table("outbound_dispatches") and not _has_column(inspector, "outbound_dispatches", "scheduled_at"):
        op.add_column("outbound_dispatches", sa.Column("scheduled_at", sa.DateTime(), nullable=True))
        op.create_index("ix_outbound_dispatches_scheduled_at", "outbound_dispatches", ["scheduled_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("outbound_dispatches"):
        if _has_index(inspector, "outbound_dispatches", "ix_outbound_dispatches_scheduled_at"):
            op.drop_index("ix_outbound_dispatches_scheduled_at", table_name="outbound_dispatches")
        if _has_column(inspector, "outbound_dispatches", "scheduled_at"):
            op.drop_column("outbound_dispatches", "scheduled_at")

    if inspector.has_table("email_drafts"):
        if _has_index(inspector, "email_drafts", "ix_email_drafts_scheduled_at"):
            op.drop_index("ix_email_drafts_scheduled_at", table_name="email_drafts")
        if _has_column(inspector, "email_drafts", "scheduled_at"):
            op.drop_column("email_drafts", "scheduled_at")
