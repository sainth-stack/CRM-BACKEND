"""Drop zombie CampaignStatus enum values never set by active pipeline.

FINDING_TARGET_COMPANIES, FINDING_DECISION_MAKERS, and DRAFTING_EMAILS were
legacy status names from the pre-STAGE_* era. The workflow has used STAGE_3/4/5/6
naming exclusively since the LangGraph rewrite; no row should carry these values
on a live deployment.

Postgres requires recreating the enum type to remove values. We:
  1. Cast any stale rows to FAILED as a safe sentinel (should be zero rows).
  2. ALTER the column to plain text temporarily.
  3. DROP + recreate the enum without the three removed values.
  4. Restore the column type.

Revision ID: a20260620_0015
Revises: a20260615_0014
Create Date: 2026-06-20 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "a20260620_0015"
down_revision = "a20260615_0014"
branch_labels = None
depends_on = None

_ZOMBIE_VALUES = ("FINDING_TARGET_COMPANIES", "FINDING_DECISION_MAKERS", "DRAFTING_EMAILS")

_LIVE_VALUES = [
    "INPUT_VALIDATED",
    "PENDING",
    "STAGE_1_CSV_TRIMMED",
    "STAGE_2_USER_INTEL_COMPLETE",
    "STAGE_3_ICP_FILTERED",
    "STAGE_4_RESEARCH_COMPLETE",
    "STAGE_5_STAKEHOLDERS_RANKED",
    "STAGE_6_DRAFTING_COMPLETE",
    "RESEARCHING_USER_COMPANY",
    "COMPLETED",
    "PARTIAL_SUCCESS",
    "INTERVENTION_NEEDED",
    "FAILED",
    "INACTIVE",
]


def upgrade():
    # Neutralise any stale rows that somehow still carry a zombie status.
    placeholders = ", ".join(f"'{v}'" for v in _ZOMBIE_VALUES)
    op.execute(
        f"UPDATE campaigns SET status = 'FAILED' WHERE status IN ({placeholders})"
    )

    # Detach column from the enum so we can drop and recreate it.
    op.execute("ALTER TABLE campaigns ALTER COLUMN status TYPE TEXT USING status::TEXT")
    op.execute("DROP TYPE IF EXISTS campaignstatus")

    # Recreate enum without the three zombie values.
    values_sql = ", ".join(f"'{v}'" for v in _LIVE_VALUES)
    op.execute(f"CREATE TYPE campaignstatus AS ENUM ({values_sql})")

    # Re-attach the column.
    op.execute(
        "ALTER TABLE campaigns ALTER COLUMN status TYPE campaignstatus "
        "USING status::campaignstatus"
    )


def downgrade():
    # Restore the three zombie values (they will simply never be set again).
    _ALL_VALUES = _LIVE_VALUES + list(_ZOMBIE_VALUES)
    op.execute("ALTER TABLE campaigns ALTER COLUMN status TYPE TEXT USING status::TEXT")
    op.execute("DROP TYPE IF EXISTS campaignstatus")
    values_sql = ", ".join(f"'{v}'" for v in _ALL_VALUES)
    op.execute(f"CREATE TYPE campaignstatus AS ENUM ({values_sql})")
    op.execute(
        "ALTER TABLE campaigns ALTER COLUMN status TYPE campaignstatus "
        "USING status::campaignstatus"
    )
