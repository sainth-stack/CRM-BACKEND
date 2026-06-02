"""Reset display_timezone default from UTC to NULL

Previously, the DecisionMaker model had default="UTC" at the ORM level,
which caused every newly discovered prospect to be stamped with UTC before
any email was dispatched. This made it impossible to distinguish between
"not yet resolved" and "confirmed UTC by geopy".

This migration resets display_timezone to NULL for all prospects that:
  - Have never had an email sent (state is NEW or DRAFTED)
  - Are currently showing UTC (from the old ORM default, not from resolution)

Prospects that have had emails dispatched keep their stored timezone.

Revision ID: a20260526_0005
Revises: a20260524_0004
Create Date: 2026-05-26 00:00:00
"""

from alembic import op

revision = "a20260526_0005"
down_revision = "a20260524_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reset the ORM-default UTC stamp for prospects that have never been dispatched.
    # States NEW and DRAFTED have never had an email sent, so their UTC is purely
    # the column default — not an actual resolved timezone.
    op.execute("""
        UPDATE decision_makers
        SET display_timezone = NULL
        WHERE display_timezone = 'UTC'
          AND state IN ('NEW', 'DRAFTED')
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE decision_makers
        SET display_timezone = 'UTC'
        WHERE display_timezone IS NULL
    """)
