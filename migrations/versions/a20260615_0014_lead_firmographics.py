"""Add richer firmographic columns to campaign_leads.

Adds the well-populated, high-signal CSV fields the ICP engine was previously
throwing away (verified fill-rates on a real upload: SIC 99%, NAICS 97%, exact
staff count / annual revenue / founded date 100%, structured country/state/city
100/98/100%). These drive deterministic industry matching (SIC/NAICS supercategory
instead of LLM-guessing "is Textiles Manufacturing?") and a genuinely-spread scale
score (exact headcount + revenue instead of coarse bands).

Sparse fields (funding 3-7%, job-change 0.4%, timezone 0%) are intentionally NOT
stored. The raw free-text `company_location` blob is superseded by the structured
country/state/city columns (the location string is reconstructed clean at load
time), so we avoid storing a redundant full-address blob alongside its parts.

Purely additive (nullable columns) — existing rows and the v3 deployment are
unaffected. Uses ADD COLUMN IF NOT EXISTS so it is safe to re-run.

Revision ID: a20260615_0014
Revises: a20260613_0013
Create Date: 2026-06-15 00:00:00
"""
from alembic import op

revision = "a20260615_0014"
down_revision = "a20260613_0013"
branch_labels = None
depends_on = None

_NEW_COLUMNS = (
    "company_sic_code",
    "company_naics_code",
    "company_staff_count",
    "company_annual_revenue",
    "company_founded_year",
    "company_country",
    "company_state",
    "company_city",
)


def upgrade() -> None:
    for col in _NEW_COLUMNS:
        op.execute(
            f"ALTER TABLE campaign_leads ADD COLUMN IF NOT EXISTS {col} VARCHAR"
        )


def downgrade() -> None:
    for col in _NEW_COLUMNS:
        op.execute(f"ALTER TABLE campaign_leads DROP COLUMN IF EXISTS {col}")
