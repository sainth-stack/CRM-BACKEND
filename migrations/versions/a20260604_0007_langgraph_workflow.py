"""Add LangGraph workflow columns to campaigns

Phase 1 of the agentic redesign introduces a LangGraph StateGraph as the
campaign stage orchestrator. This adds two bookkeeping columns:
  * workflow_version    — pins a running campaign to a workflow definition
                          (workflow-versioning constraint).
  * workflow_thread_id  — the checkpointer thread key (defaults to campaign id).

The LangGraph checkpoint tables (checkpoints, checkpoint_writes,
checkpoint_blobs, checkpoint_migrations) are created and owned by
PostgresSaver.setup() at runtime, NOT by this migration, to avoid drift with the
library's own schema.

Revision ID: a20260604_0007
Revises: a20260603_0006
Create Date: 2026-06-04 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "a20260604_0007"
down_revision = "a20260603_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(
            sa.Column("workflow_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("workflow_thread_id", sa.String(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_column("workflow_thread_id")
        batch_op.drop_column("workflow_version")
