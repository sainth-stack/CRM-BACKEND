import uuid
import datetime
from datetime import UTC
from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from .base import Base


class CampaignLead(Base):
    """One normalized row per CSV contact for a campaign.

    Replaces the old single trimmed-CSV text blob (`Campaign.trimmed_csv_data`).
    The upload is parsed ONCE at ingestion into these typed columns; Stages 3 and
    5 then read structured rows (chunked) instead of re-parsing the blob with
    pandas on every run — flat memory, no repeated parse, no local file.

    Column names mirror the canonical field names the CSV fuzzy-mapper produces, so
    the dicts rebuilt for the downstream stages are identical to what the legacy
    `process_csv_content` returned. `id` is the sole PK (UUID); `campaign_id` is a
    FK (the project-wide convention — campaign_id/user_id are never primary keys).
    """
    __tablename__ = "campaign_leads"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False)

    # ---- Company cluster (the dedup anchor + firmographics) ----
    domain = Column(String, index=True)            # normalized website domain (anchor key)
    website_raw = Column(String)                   # raw domain/website value as supplied
    company_name_cleaned = Column(String)
    company_location = Column(String)              # DEPRECATED raw blob; superseded by country/state/city
    company_industry = Column(String)
    company_staff_count_range = Column(String)
    company_description = Column(Text)
    company_revenue_range = Column(String)
    company_li_profile_url = Column(String)
    company_linkedin_id = Column(String)
    # Richer firmographics (high fill-rate CSV fields the ICP engine now uses).
    company_sic_code = Column(String)              # deterministic industry (SIC supercategory)
    company_naics_code = Column(String)            # deterministic industry (NAICS supercategory)
    company_staff_count = Column(String)           # EXACT headcount -> continuous scale score
    company_annual_revenue = Column(String)        # EXACT revenue -> continuous scale score
    company_founded_year = Column(String)          # company age / maturity signal
    company_country = Column(String)               # structured location (drives ICP location match)
    company_state = Column(String)
    company_city = Column(String)

    # ---- Contact cluster ----
    contact_full_name = Column(String)
    title = Column(String)
    seniority = Column(String)
    department = Column(String)
    primary_email = Column(String, index=True)     # NULL => company-only row (no emailable contact)
    contact_location = Column(String)
    contact_li_profile_url = Column(String)
    contact_mobile_phone = Column(String)
    company_phone_1 = Column(String)
    time_in_role = Column(String)
    time_at_company = Column(String)

    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    campaign = relationship("Campaign", back_populates="leads")
