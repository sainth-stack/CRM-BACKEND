"""Add DecisionMaker.last_rfc_message_id for correct email threading.

The In-Reply-To/References headers need the RFC5322 Message-ID of the previous
email, not Gmail's internal thread_id (a different kind of identifier). This
column stores that value so every subsequent email to the same prospect threads
correctly across all mail clients, not just Gmail's own conversation view.

Revision ID: a20260627_0019
Revises: a20260626_0018
Create Date: 2026-06-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a20260627_0019"
down_revision: Union[str, None] = "a20260626_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("decision_makers", "last_rfc_message_id"):
        op.add_column(
            "decision_makers",
            sa.Column("last_rfc_message_id", sa.String(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("decision_makers", "last_rfc_message_id"):
        op.drop_column("decision_makers", "last_rfc_message_id")
