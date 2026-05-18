"""stage1 input validation contract

Revision ID: a20260426_0008
Revises: e4084b1ac1e1
Create Date: 2026-04-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a20260426_0008"
down_revision: Union[str, None] = "e4084b1ac1e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE campaignstatus ADD VALUE IF NOT EXISTS 'INPUT_VALIDATED'")

    if _has_column("campaigns", "user_query"):
        op.drop_column("campaigns", "user_query")

    if not _has_column("campaigns", "input_validation_review"):
        op.add_column("campaigns", sa.Column("input_validation_review", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("campaigns", "input_validation_review"):
        op.drop_column("campaigns", "input_validation_review")
