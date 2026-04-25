"""discovery expired state

Revision ID: a20260424_0004
Revises: a20260424_0003
Create Date: 2026-04-24 15:30:00
"""

from alembic import op


revision = "a20260424_0004"
down_revision = "a20260424_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS 'DISCOVERY_EXPIRED'")


def downgrade() -> None:
    # Postgres enum values are intentionally left in place on downgrade.
    pass
