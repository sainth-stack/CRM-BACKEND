"""production hardening

Revision ID: a20260423_0002
Revises: a20260423_0001
Create Date: 2026-04-23 23:10:00
"""

from alembic import op
import sqlalchemy as sa

from app.db import models


revision = "a20260423_0002"
down_revision = "a20260423_0001"
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

    if inspector.has_table("oauth_accounts") and not _has_unique(
        inspector,
        "oauth_accounts",
        "_user_provider_oauth_uc",
    ):
        op.create_unique_constraint(
            "_user_provider_oauth_uc",
            "oauth_accounts",
            ["user_id", "provider"],
        )

    if inspector.has_table("decision_makers") and not _has_index(
        inspector,
        "decision_makers",
        "ix_decision_makers_thread_id",
    ):
        op.create_index("ix_decision_makers_thread_id", "decision_makers", ["thread_id"], unique=False)

    inspector = sa.inspect(bind)
    if not inspector.has_table("prospect_lifecycle_transitions"):
        op.create_table(
            "prospect_lifecycle_transitions",
            sa.Column("id", sa.String(), primary_key=True, nullable=False),
            sa.Column("campaign_id", sa.String(), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
            sa.Column("dm_id", sa.String(), sa.ForeignKey("decision_makers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("from_state", sa.Enum(models.ProspectState, name="prospectstate"), nullable=True),
            sa.Column("to_state", sa.Enum(models.ProspectState, name="prospectstate"), nullable=True),
            sa.Column("from_status", sa.String(), nullable=True),
            sa.Column("to_status", sa.String(), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("actor", sa.String(), nullable=False, server_default="system"),
            sa.Column("metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_prospect_lifecycle_transitions_campaign_id",
            "prospect_lifecycle_transitions",
            ["campaign_id"],
            unique=False,
        )
        op.create_index(
            "ix_prospect_lifecycle_transitions_dm_id",
            "prospect_lifecycle_transitions",
            ["dm_id"],
            unique=False,
        )
        op.create_index(
            "ix_prospect_lifecycle_transitions_to_state",
            "prospect_lifecycle_transitions",
            ["to_state"],
            unique=False,
        )
        op.create_index(
            "ix_prospect_lifecycle_transitions_reason",
            "prospect_lifecycle_transitions",
            ["reason"],
            unique=False,
        )
        op.create_index(
            "ix_prospect_lifecycle_transitions_created_at",
            "prospect_lifecycle_transitions",
            ["created_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("prospect_lifecycle_transitions"):
        for index_name in [
            "ix_prospect_lifecycle_transitions_created_at",
            "ix_prospect_lifecycle_transitions_reason",
            "ix_prospect_lifecycle_transitions_to_state",
            "ix_prospect_lifecycle_transitions_dm_id",
            "ix_prospect_lifecycle_transitions_campaign_id",
        ]:
            if _has_index(inspector, "prospect_lifecycle_transitions", index_name):
                op.drop_index(index_name, table_name="prospect_lifecycle_transitions")
        op.drop_table("prospect_lifecycle_transitions")

    inspector = sa.inspect(bind)
    if inspector.has_table("decision_makers") and _has_index(
        inspector,
        "decision_makers",
        "ix_decision_makers_thread_id",
    ):
        op.drop_index("ix_decision_makers_thread_id", table_name="decision_makers")

    if inspector.has_table("oauth_accounts") and _has_unique(
        inspector,
        "oauth_accounts",
        "_user_provider_oauth_uc",
    ):
        op.drop_constraint("_user_provider_oauth_uc", "oauth_accounts", type_="unique")
