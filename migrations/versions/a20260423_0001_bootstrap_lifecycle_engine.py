"""bootstrap lifecycle engine

Revision ID: a20260423_0001
Revises:
Create Date: 2026-04-23 21:30:00
"""

from alembic import op
import sqlalchemy as sa

from app.db import models


# revision identifiers, used by Alembic.
revision = "a20260423_0001"
down_revision = "970556d479bf"
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

    # Fresh deployments: migrations should handle table creation.
    # models.Base.metadata.create_all(bind=bind)

    inspector = sa.inspect(bind)

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS 'ON_HOLD'")

    termination_enum = sa.Enum(models.ProspectTerminationReason, name="prospectterminationreason")
    if bind.dialect.name == "postgresql":
        termination_enum.create(bind, checkfirst=True)

    if inspector.has_table("decision_makers"):
        if not _has_column(inspector, "decision_makers", "termination_reason"):
            op.add_column("decision_makers", sa.Column("termination_reason", termination_enum, nullable=True))
        if not _has_column(inspector, "decision_makers", "hold_source_dm_id"):
            op.add_column("decision_makers", sa.Column("hold_source_dm_id", sa.String(), nullable=True))
        if not _has_column(inspector, "decision_makers", "held_at"):
            op.add_column("decision_makers", sa.Column("held_at", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "decision_makers", "hold_release_at"):
            op.add_column("decision_makers", sa.Column("hold_release_at", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "decision_makers", "pre_hold_state"):
            op.add_column("decision_makers", sa.Column("pre_hold_state", sa.Enum(models.ProspectState, name="prospectstate"), nullable=True))
        if not _has_column(inspector, "decision_makers", "pre_hold_status"):
            op.add_column("decision_makers", sa.Column("pre_hold_status", sa.String(), nullable=True))
        if not _has_column(inspector, "decision_makers", "pre_hold_next_action_at"):
            op.add_column("decision_makers", sa.Column("pre_hold_next_action_at", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "decision_makers", "state"):
            op.add_column("decision_makers", sa.Column("state", sa.Enum(models.ProspectState, name="prospectstate"), nullable=True))
        if not _has_column(inspector, "decision_makers", "next_action_at"):
            op.add_column("decision_makers", sa.Column("next_action_at", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "decision_makers", "retry_after"):
            op.add_column("decision_makers", sa.Column("retry_after", sa.DateTime(), nullable=True))

        inspector = sa.inspect(bind)
        if not _has_index(inspector, "decision_makers", "ix_decision_makers_next_action_at"):
            op.create_index("ix_decision_makers_next_action_at", "decision_makers", ["next_action_at"], unique=False)
        if not _has_index(inspector, "decision_makers", "ix_decision_makers_retry_after"):
            op.create_index("ix_decision_makers_retry_after", "decision_makers", ["retry_after"], unique=False)
        if not _has_index(inspector, "decision_makers", "ix_decision_makers_termination_reason"):
            op.create_index("ix_decision_makers_termination_reason", "decision_makers", ["termination_reason"], unique=False)
        if not _has_index(inspector, "decision_makers", "ix_decision_makers_hold_source_dm_id"):
            op.create_index("ix_decision_makers_hold_source_dm_id", "decision_makers", ["hold_source_dm_id"], unique=False)
        if not _has_index(inspector, "decision_makers", "ix_decision_makers_hold_release_at"):
            op.create_index("ix_decision_makers_hold_release_at", "decision_makers", ["hold_release_at"], unique=False)

    if inspector.has_table("campaigns"):
        if not _has_column(inspector, "campaigns", "last_heartbeat"):
            op.add_column("campaigns", sa.Column("last_heartbeat", sa.DateTime(), nullable=True))
        if not _has_column(inspector, "campaigns", "locked_by"):
            op.add_column("campaigns", sa.Column("locked_by", sa.String(), nullable=True))

    inspector = sa.inspect(bind)
    if inspector.has_table("communication_logs") and not _has_unique(
        inspector,
        "communication_logs",
        "_communication_message_direction_uc",
    ):
        op.create_unique_constraint(
            "_communication_message_direction_uc",
            "communication_logs",
            ["message_id", "direction"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("communication_logs") and _has_unique(
        inspector,
        "communication_logs",
        "_communication_message_direction_uc",
    ):
        op.drop_constraint("_communication_message_direction_uc", "communication_logs", type_="unique")

    if inspector.has_table("decision_makers"):
        for index_name in [
            "ix_decision_makers_hold_release_at",
            "ix_decision_makers_hold_source_dm_id",
            "ix_decision_makers_termination_reason",
            "ix_decision_makers_retry_after",
            "ix_decision_makers_next_action_at",
        ]:
            if _has_index(inspector, "decision_makers", index_name):
                op.drop_index(index_name, table_name="decision_makers")

        for column_name in [
            "pre_hold_next_action_at",
            "pre_hold_status",
            "pre_hold_state",
            "hold_release_at",
            "held_at",
            "hold_source_dm_id",
            "termination_reason",
        ]:
            if _has_column(inspector, "decision_makers", column_name):
                op.drop_column("decision_makers", column_name)

    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS prospectterminationreason")
