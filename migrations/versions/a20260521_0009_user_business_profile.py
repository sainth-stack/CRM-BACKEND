"""user business profile fields

Revision ID: a20260521_0009
Revises: 5c2da5eea864
Create Date: 2026-05-21

Re-parented onto 5c2da5eea864 (the prior head) instead of a20260426_0008
to linearize history after the sibling-head divergence.

Also defensively adds the cal_* columns introduced earlier without a migration
(they only ever landed on fresh databases via Base.metadata.create_all in
init_db.py). All add_column calls are guarded by _has_column so this migration
is a no-op for columns that are already present.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "a20260521_0009"
down_revision: Union[str, None] = "5c2da5eea864"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


# Columns this migration ensures exist on `users`. (column_name, SQLAlchemy type)
_USERS_COLUMNS = [
    # Business identity profile (used by Ghostwriter agent for AI email personalization)
    ("full_name", sa.String()),
    ("company_name", sa.String()),
    ("role_title", sa.String()),
    # Cal.com OAuth & scheduling settings (added to model previously without a migration)
    ("cal_access_token", sa.String()),
    ("cal_refresh_token", sa.String()),
    ("cal_token_expires_at", sa.DateTime()),
    ("cal_event_type_id", sa.Integer()),
    ("cal_timezone", sa.String()),
]


def upgrade() -> None:
    for name, col_type in _USERS_COLUMNS:
        if not _has_column("users", name):
            op.add_column("users", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    # Only drop business-profile fields; cal_* were pre-existing in most environments
    # and dropping them here would be destructive on rollback.
    for name in ("role_title", "company_name", "full_name"):
        if _has_column("users", name):
            op.drop_column("users", name)
