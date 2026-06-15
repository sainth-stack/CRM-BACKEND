"""Normalized lead storage: parse the trimmed CSV ONCE into campaign_leads rows,
then rebuild the dicts the downstream stages expect by reading those rows.

This replaces the pattern where Stage 3 and Stage 5 each re-parsed the trimmed-CSV
text blob with pandas (full batch resident in RAM, parsed twice). Now the CSV is
parsed a single time at ingestion; the stages read structured rows in chunks.

The dicts returned by `load_unique_cos` (Stage 3) and `load_contacts_for_domains`
(Stage 5, per-chunk) are byte-compatible with what the legacy
`CSVProcessingService.process_csv_content` produced, so the stage logic and the
stakeholder ranker need no changes.
"""
from __future__ import annotations

import uuid
import datetime
from datetime import UTC

from sqlalchemy.orm import Session

from app.db import models
from app.core.logging_config import logger
from app.services.csv_service import CSVProcessingService

# How many lead rows to flush per INSERT batch (keeps memory + transaction size flat).
_INSERT_CHUNK = 500

# Contact-cluster columns emitted into each rebuilt prospect dict (only when non-NULL,
# mirroring the legacy NaN-drop). These cover every key Stage 5 + the stakeholder
# ranker read.
_CONTACT_FIELDS = (
    "contact_full_name", "title", "seniority", "department", "primary_email",
    "contact_location", "company_location", "contact_li_profile_url",
    "contact_mobile_phone", "company_phone_1", "time_in_role", "time_at_company",
    "company_name_cleaned",
)


def has_leads(db: Session, campaign_id: str) -> bool:
    """True if this campaign has already been normalized into campaign_leads."""
    return db.query(models.CampaignLead.id).filter(
        models.CampaignLead.campaign_id == campaign_id
    ).first() is not None


def _company_row_fields(domain: str, co: dict) -> dict:
    """Map a unique_cos value -> CampaignLead company columns.

    The raw `company_location` blob is intentionally NOT stored — the structured
    country/state/city columns are the source of truth and the clean location string
    is reconstructed from them in `load_unique_cos` (no redundant full-address blob)."""
    return {
        "domain": domain,
        "website_raw": co.get("website"),
        "company_name_cleaned": co.get("name"),
        "company_industry": co.get("industry"),
        "company_staff_count_range": co.get("size"),
        "company_description": co.get("description"),
        "company_revenue_range": co.get("revenue"),
        "company_li_profile_url": co.get("linkedin"),
        "company_linkedin_id": co.get("linkedin_id"),
        # Richer firmographics.
        "company_sic_code": co.get("sic_code"),
        "company_naics_code": co.get("naics_code"),
        "company_staff_count": co.get("staff_count"),
        "company_annual_revenue": co.get("annual_revenue"),
        "company_founded_year": co.get("founded_year"),
        "company_country": co.get("country"),
        "company_state": co.get("state"),
        "company_city": co.get("city"),
    }


def persist_leads(db: Session, campaign_id: str, contacts_map: dict, unique_cos: dict) -> int:
    """Insert one campaign_leads row per contact (chunked bulk insert).

    A company with no emailable contact still gets one company-only row (contact
    columns NULL) so Stage 3 can still ICP-evaluate it — matching the legacy
    behaviour where unique_cos contained such companies but contacts_map did not.
    Returns the number of rows inserted.
    """
    mappings: list[dict] = []
    inserted = 0

    def _flush():
        nonlocal inserted, mappings
        if mappings:
            db.bulk_insert_mappings(models.CampaignLead, mappings)
            db.flush()
            inserted += len(mappings)
            mappings = []

    now = datetime.datetime.now(UTC)
    for domain, co in unique_cos.items():
        co_fields = _company_row_fields(domain, co)
        prospects = contacts_map.get(domain, [])

        if not prospects:
            # Company-only row (no emailable contact).
            row = {"id": str(uuid.uuid4()), "campaign_id": campaign_id, "created_at": now}
            row.update(co_fields)
            mappings.append(row)
        else:
            for p in prospects:
                row = {"id": str(uuid.uuid4()), "campaign_id": campaign_id, "created_at": now}
                row.update(co_fields)
                row.update({
                    "contact_full_name": p.get("contact_full_name"),
                    "title": p.get("title"),
                    "seniority": p.get("seniority"),
                    "department": p.get("department"),
                    "primary_email": p.get("primary_email"),
                    "contact_location": p.get("contact_location"),
                    "contact_li_profile_url": p.get("contact_li_profile_url"),
                    "contact_mobile_phone": p.get("contact_mobile_phone"),
                    "company_phone_1": p.get("company_phone_1"),
                    "time_in_role": p.get("time_in_role"),
                    "time_at_company": p.get("time_at_company"),
                })
                mappings.append(row)

        if len(mappings) >= _INSERT_CHUNK:
            _flush()

    _flush()
    return inserted


def normalize_csv_to_leads(db: Session, campaign_id: str, csv_text: str) -> int:
    """Parse a trimmed-CSV string ONCE and persist its rows. Idempotent: if the
    campaign already has leads, do nothing. Returns rows inserted (0 if skipped)."""
    if not csv_text or has_leads(db, campaign_id):
        return 0
    svc = CSVProcessingService()
    contacts_map, unique_cos = svc.process_csv_content(
        csv_text.encode("utf-8"), None, None, None, campaign_id, db
    )
    n = persist_leads(db, campaign_id, contacts_map, unique_cos)
    logger.info(f"📥 [LEADS] Normalized {n} lead row(s) for {campaign_id}.")
    return n


def load_unique_cos(db: Session, campaign_id: str) -> dict:
    """Rebuild the legacy unique_cos dict (one entry per distinct company) from
    campaign_leads. Streams rows; keeps one entry per domain."""
    unique_cos: dict = {}
    q = (
        db.query(models.CampaignLead)
        .filter(models.CampaignLead.campaign_id == campaign_id)
        .yield_per(_INSERT_CHUNK)
    )
    for r in q:
        if not r.domain or r.domain in unique_cos:
            continue
        # Reconstruct a clean location from the structured parts; fall back to the
        # legacy blob for old rows ingested before the firmographic columns existed.
        clean_loc = ", ".join(
            p for p in (r.company_city, r.company_state, r.company_country) if p
        ) or r.company_location
        unique_cos[r.domain] = {
            "name": r.company_name_cleaned or r.domain,
            "domain": r.domain,
            "location": clean_loc,
            "industry": r.company_industry,
            "size": r.company_staff_count_range,
            "description": r.company_description,
            "revenue": r.company_revenue_range,
            "linkedin": r.company_li_profile_url,
            "linkedin_id": r.company_linkedin_id,
            "website": r.website_raw,
            # Richer firmographics for ICP scoring.
            "sic_code": r.company_sic_code,
            "naics_code": r.company_naics_code,
            "staff_count": r.company_staff_count,
            "annual_revenue": r.company_annual_revenue,
            "founded_year": r.company_founded_year,
            "country": r.company_country,
            "state": r.company_state,
            "city": r.company_city,
        }
    return unique_cos


def load_contacts_for_domains(db: Session, campaign_id: str, domains) -> dict:
    """Contacts (primary_email present) for a SET of domains -> {domain: [prospects]}.

    Stage 5 uses this to load just one chunk's companies at a time, so the whole
    contacts_map is never resident — memory stays flat regardless of batch size.
    """
    domains = [d for d in {(x or "").strip().lower() for x in domains} if d]
    if not domains:
        return {}
    rows = (
        db.query(models.CampaignLead)
        .filter(
            models.CampaignLead.campaign_id == campaign_id,
            models.CampaignLead.domain.in_(domains),
            models.CampaignLead.primary_email.isnot(None),
        )
        .all()
    )
    out: dict = {}
    for r in rows:
        prospect = {f: getattr(r, f) for f in _CONTACT_FIELDS if getattr(r, f) not in (None, "")}
        if prospect.get("primary_email"):
            out.setdefault(r.domain, []).append(prospect)
    return out
