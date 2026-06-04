"""Workflow state schema.

These are pure *routing* flags, re-derived from the database on every graph
invocation (the DB remains the source of truth). The graph never mutates domain
data through this state — it only decides which stage to dispatch next.
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class CampaignWorkflowState(TypedDict, total=False):
    campaign_id: str

    # --- Stage completion flags (mirrors legacy process_state_machine) ---
    val_done: bool          # validation passed (stage-1 review success OR Agent-B is_valid)
    val_failed: bool        # validation needs clarification / rejected
    csv_done: bool          # trimmed CSV SSoT present
    intel_done: bool        # user-intel v2 dossier present
    icp_done: bool          # status >= STAGE_3_ICP_FILTERED
    research_done: bool     # status >= STAGE_4_RESEARCH_COMPLETE
    ranking_done: bool      # status >= STAGE_5_STAKEHOLDERS_RANKED
    drafting_done: bool     # status >= STAGE_6 / COMPLETED / terminal

    # --- Audit: which dispatch nodes fired this invocation (reducer-merged) ---
    dispatched: Annotated[list, operator.add]
