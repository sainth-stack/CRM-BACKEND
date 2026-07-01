"""Add TargetCompany.screener_reasoning so screener verdicts survive ICP overwrite.

_update_icp_batch unconditionally overwrites relevance_explanation with the ICP
agent's reasoning, which permanently destroyed the LeadFitScreener's per-check
evidence for every company that passed the screener. That evidence never
reached the Targets card popup because the field it lived in got clobbered
before the popup ever read it. This column gives the screener its own
persistent home, independent of relevance_explanation.

Revision ID: a20260701_0020
Revises: a20260627_0019
Create Date: 2026-07-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a20260701_0020"
down_revision: Union[str, None] = "a20260627_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("target_companies", "screener_reasoning"):
        op.add_column(
            "target_companies",
            sa.Column("screener_reasoning", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("target_companies", "screener_reasoning"):
        op.drop_column("target_companies", "screener_reasoning")
