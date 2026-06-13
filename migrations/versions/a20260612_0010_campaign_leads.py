"""Add campaign_leads table (normalized CSV storage).

Replaces the single trimmed-CSV text blob (campaigns.trimmed_csv_data) with one
typed row per CSV contact. The upload is parsed ONCE at ingestion into these
columns; Stages 3 and 5 read structured rows in chunks instead of re-parsing the
blob with pandas on every run (flat memory, no repeated parse, no local file).

Purely additive — creates a new table + indexes only. Existing tables (shared with
the v3 deployment) are untouched, so v3 is unaffected. Uses IF NOT EXISTS so it is
safe to re-run / matches metadata create_all on a fresh DB.

Revision ID: a20260612_0010
Revises: a20260605_0008
Create Date: 2026-06-12 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "a20260612_0010"
down_revision = "a20260605_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_leads",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Company cluster
        sa.Column("domain", sa.String(), nullable=True),
        sa.Column("website_raw", sa.String(), nullable=True),
        sa.Column("company_name_cleaned", sa.String(), nullable=True),
        sa.Column("company_location", sa.String(), nullable=True),
        sa.Column("company_industry", sa.String(), nullable=True),
        sa.Column("company_staff_count_range", sa.String(), nullable=True),
        sa.Column("company_description", sa.Text(), nullable=True),
        sa.Column("company_revenue_range", sa.String(), nullable=True),
        sa.Column("company_li_profile_url", sa.String(), nullable=True),
        sa.Column("company_linkedin_id", sa.String(), nullable=True),
        # Contact cluster
        sa.Column("contact_full_name", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("seniority", sa.String(), nullable=True),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("primary_email", sa.String(), nullable=True),
        sa.Column("contact_location", sa.String(), nullable=True),
        sa.Column("contact_li_profile_url", sa.String(), nullable=True),
        sa.Column("contact_mobile_phone", sa.String(), nullable=True),
        sa.Column("company_phone_1", sa.String(), nullable=True),
        sa.Column("time_in_role", sa.String(), nullable=True),
        sa.Column("time_at_company", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_leads_campaign_id "
        "ON campaign_leads (campaign_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_leads_domain ON campaign_leads (domain)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_leads_primary_email "
        "ON campaign_leads (primary_email)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_campaign_leads_primary_email")
    op.execute("DROP INDEX IF EXISTS ix_campaign_leads_domain")
    op.execute("DROP INDEX IF EXISTS ix_campaign_leads_campaign_id")
    op.drop_table("campaign_leads")
