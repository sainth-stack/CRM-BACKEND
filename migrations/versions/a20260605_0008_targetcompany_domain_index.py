"""Add index on target_companies.domain for the ICP research cache lookup.

The ICP gate reuses prior research per *domain* across campaigns
(`CompanyValidationService.prefetch_icp_research` / `_gather_research`), querying
`WHERE domain = ? AND icp_research_context IS NOT NULL ORDER BY updated_at DESC`.

The only existing index covering `domain` is the composite UNIQUE
(`campaign_id`, `domain`), whose leading column is `campaign_id` — it cannot
serve a `domain`-only predicate, so the cache lookup was a sequential scan that
got slower as the global table grew (an N+1 across the whole campaign). This adds
a standalone btree index on `domain`.

Idempotent: uses CREATE INDEX IF NOT EXISTS so it is safe to re-run and matches
the model's `index=True` (auto-named `ix_target_companies_domain`).

Revision ID: a20260605_0008
Revises: a20260604_0007
Create Date: 2026-06-05 00:00:00
"""
from alembic import op

revision = "a20260605_0008"
down_revision = "a20260604_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this safe across environments where the index may have
    # been created out-of-band (e.g. by metadata create_all on a fresh DB).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_target_companies_domain "
        "ON target_companies (domain)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_target_companies_domain")
