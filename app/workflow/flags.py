"""Derive routing flags from the database.

This is a faithful, side-effect-free re-implementation of the boolean snapshot
that legacy `process_state_machine` computed inline (campaign_service.py
~lines 598-616), plus the status-derived flags for stages 3-6.
"""
from __future__ import annotations

from typing import Optional

from app.db import models
from app.workers.utils import db_session

# Statuses at/after each stage boundary. Membership => that stage is complete.
_ICP_DONE = {
    models.CampaignStatus.STAGE_3_ICP_FILTERED,
    models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE,
    models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED,
    models.CampaignStatus.STAGE_6_DRAFTING_COMPLETE,
    models.CampaignStatus.COMPLETED,
    models.CampaignStatus.PARTIAL_SUCCESS,
    models.CampaignStatus.FAILED,
    models.CampaignStatus.INACTIVE,
}
_RESEARCH_DONE = {
    models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE,
    models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED,
    models.CampaignStatus.STAGE_6_DRAFTING_COMPLETE,
    models.CampaignStatus.COMPLETED,
    models.CampaignStatus.PARTIAL_SUCCESS,
    models.CampaignStatus.FAILED,
    models.CampaignStatus.INACTIVE,
}
_RANKING_DONE = {
    models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED,
    models.CampaignStatus.STAGE_6_DRAFTING_COMPLETE,
    models.CampaignStatus.COMPLETED,
    models.CampaignStatus.PARTIAL_SUCCESS,
    models.CampaignStatus.FAILED,
    models.CampaignStatus.INACTIVE,
}
_DRAFTING_DONE = {
    models.CampaignStatus.STAGE_6_DRAFTING_COMPLETE,
    models.CampaignStatus.COMPLETED,
    models.CampaignStatus.PARTIAL_SUCCESS,
    models.CampaignStatus.FAILED,
    models.CampaignStatus.INACTIVE,
}


def read_flags(campaign_id: str) -> Optional[dict]:
    """Return the routing-flag snapshot for a campaign, or None if it is gone."""
    with db_session() as db:
        campaign = (
            db.query(models.Campaign)
            .filter(models.Campaign.id == campaign_id)
            .first()
        )
        if not campaign:
            return None

        val_review = campaign.input_validation_review
        if val_review:
            is_valid_flag = val_review.get("is_valid")
            status_val = (val_review.get("overall") or {}).get("status")
            val_done = (is_valid_flag is True) or (status_val == "success")
            val_failed = (is_valid_flag is False) or (status_val == "needs_clarification")
        else:
            val_done = False
            val_failed = False

        # CSV is "done" once it is normalized into campaign_leads rows. The legacy
        # blob / csv_file_url are kept as fallbacks for campaigns created before the
        # normalization existed (so a half-migrated campaign still routes forward).
        leads_present = (
            db.query(models.CampaignLead.id)
            .filter(models.CampaignLead.campaign_id == campaign_id)
            .first()
            is not None
        )
        csv_done = leads_present or (campaign.trimmed_csv_data is not None) or (campaign.csv_file_url is not None)
        intel_done = (
            campaign.user_intel is not None and campaign.user_intel.v2_intel is not None
        )

        status = campaign.status
        return {
            "val_done": val_done,
            "val_failed": val_failed,
            "csv_done": csv_done,
            "intel_done": intel_done,
            "icp_done": status in _ICP_DONE,
            "research_done": status in _RESEARCH_DONE,
            "ranking_done": status in _RANKING_DONE,
            "drafting_done": status in _DRAFTING_DONE,
        }
