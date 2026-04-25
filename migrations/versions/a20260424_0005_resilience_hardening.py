"""resilience hardening

Revision ID: a20260424_0005
Revises: a20260424_0004
Create Date: 2026-04-24 22:15:00
"""

from alembic import op
import sqlalchemy as sa


revision = "a20260424_0005"
down_revision = "a20260424_0004"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _has_unique(inspector, table_name: str, constraint_name: str) -> bool:
    return any(constraint["name"] == constraint_name for constraint in inspector.get_unique_constraints(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("oauth_accounts"):
        if not _has_column(inspector, "oauth_accounts", "mailbox_health_status"):
            op.add_column(
                "oauth_accounts",
                sa.Column("mailbox_health_status", sa.String(), nullable=True, server_default="UNKNOWN"),
            )
        if not _has_column(inspector, "oauth_accounts", "mailbox_last_checked_at"):
            op.add_column("oauth_accounts", sa.Column("mailbox_last_checked_at", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "oauth_accounts", "mailbox_last_error"):
            op.add_column("oauth_accounts", sa.Column("mailbox_last_error", sa.Text(), nullable=True))

        bind.execute(
            sa.text(
                "UPDATE oauth_accounts "
                "SET mailbox_health_status = COALESCE(mailbox_health_status, 'UNKNOWN')"
            )
        )

        inspector = sa.inspect(bind)
        if not _has_index(inspector, "oauth_accounts", "ix_oauth_accounts_mailbox_health_status"):
            op.create_index(
                "ix_oauth_accounts_mailbox_health_status",
                "oauth_accounts",
                ["mailbox_health_status"],
                unique=False,
            )

    inspector = sa.inspect(bind)
    if not inspector.has_table("outbound_dispatches"):
        op.create_table(
            "outbound_dispatches",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("campaign_id", sa.String(), nullable=False),
            sa.Column("dm_id", sa.String(), nullable=False),
            sa.Column("draft_id", sa.String(), nullable=True),
            sa.Column("action_type", sa.String(), nullable=False),
            sa.Column("dispatch_key", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=True, server_default="IDLE"),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.Column("thread_id", sa.String(), nullable=True),
            sa.Column("dispatch_started_at", sa.DateTime(), nullable=True),
            sa.Column("dispatch_completed_at", sa.DateTime(), nullable=True),
            sa.Column("dispatch_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["dm_id"], ["decision_makers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["draft_id"], ["email_drafts.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    if not _has_unique(inspector, "outbound_dispatches", "_outbound_dispatch_key_uc"):
        op.create_unique_constraint(
            "_outbound_dispatch_key_uc",
            "outbound_dispatches",
            ["dispatch_key"],
        )

    for index_name, columns in [
        ("ix_outbound_dispatches_campaign_id", ["campaign_id"]),
        ("ix_outbound_dispatches_dm_id", ["dm_id"]),
        ("ix_outbound_dispatches_draft_id", ["draft_id"]),
        ("ix_outbound_dispatches_action_type", ["action_type"]),
        ("ix_outbound_dispatches_dispatch_key", ["dispatch_key"]),
        ("ix_outbound_dispatches_state", ["state"]),
        ("ix_outbound_dispatches_created_at", ["created_at"]),
    ]:
        inspector = sa.inspect(bind)
        if not _has_index(inspector, "outbound_dispatches", index_name):
            op.create_index(index_name, "outbound_dispatches", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("outbound_dispatches"):
        for index_name in [
            "ix_outbound_dispatches_created_at",
            "ix_outbound_dispatches_state",
            "ix_outbound_dispatches_dispatch_key",
            "ix_outbound_dispatches_action_type",
            "ix_outbound_dispatches_draft_id",
            "ix_outbound_dispatches_dm_id",
            "ix_outbound_dispatches_campaign_id",
        ]:
            inspector = sa.inspect(bind)
            if _has_index(inspector, "outbound_dispatches", index_name):
                op.drop_index(index_name, table_name="outbound_dispatches")

        inspector = sa.inspect(bind)
        if _has_unique(inspector, "outbound_dispatches", "_outbound_dispatch_key_uc"):
            op.drop_constraint("_outbound_dispatch_key_uc", "outbound_dispatches", type_="unique")
        op.drop_table("outbound_dispatches")

    inspector = sa.inspect(bind)
    if inspector.has_table("oauth_accounts"):
        if _has_index(inspector, "oauth_accounts", "ix_oauth_accounts_mailbox_health_status"):
            op.drop_index("ix_oauth_accounts_mailbox_health_status", table_name="oauth_accounts")
        for column_name in [
            "mailbox_last_error",
            "mailbox_last_checked_at",
            "mailbox_health_status",
        ]:
            inspector = sa.inspect(bind)
            if _has_column(inspector, "oauth_accounts", column_name):
                op.drop_column("oauth_accounts", column_name)
