"""The campaign workflow StateGraph.

Routing is a faithful translation of legacy `process_state_machine`:

  Legacy early block (status in PENDING/INPUT_VALIDATED/STAGE_1/STAGE_2/
  RESEARCHING/INTERVENTION):
    - input validation runs synchronously in the API (single validator); the
      persisted review sets val_done before the campaign enters the graph
    - if not csv_done                     -> process_csv_worker
    - if val_done and not intel_done      -> (status=RESEARCHING) research_user_company_worker
    - barrier: wait until val+csv+intel   -> else find_companies_worker (status=STAGE_2)
  Stage transitions:
    - dispatch_icp (find_companies_worker) merges ICP + deep research and advances
      straight to STAGE_4_RESEARCH_COMPLETE (no separate deep-research dispatch)
    - STAGE_4_RESEARCH_COMPLETE  -> find_dms_worker
    - STAGE_5_STAKEHOLDERS_RANKED-> draft_emails_worker
  Failure:
    - val_failed                 -> status=INTERVENTION_NEEDED (pause)

Each dispatch node opens its own short DB session to set the same interim
status the legacy code set, then enqueues the existing Celery worker. Workers
keep their own Redis locks + idempotency gates, so double-dispatch is a no-op.
"""
from __future__ import annotations

from langgraph.graph import StateGraph, START, END

from app.core.logging_config import logger
from app.db import models
from app.workers.utils import db_session
from app.workflow.state import CampaignWorkflowState


# --------------------------------------------------------------------------- #
# Routing                                                                      #
# --------------------------------------------------------------------------- #
def route(state: CampaignWorkflowState):
    """Pick the next dispatch node(s) from the flag snapshot. May return a list
    (fan-out) for the parallel validate/csv/intel tracks."""
    if state.get("val_failed"):
        return "input_gate"

    if not state.get("icp_done"):
        # Pre-Stage-3 early block (parallel tracks). Input validation is no longer
        # a dispatched track — it runs synchronously in the API and its persisted
        # review already sets val_done before the campaign enters the graph.
        early: list[str] = []
        if not state.get("csv_done"):
            early.append("dispatch_csv")
        if state.get("val_done") and not state.get("intel_done"):
            early.append("dispatch_intel")
        if early:
            return early

        # Barrier: need validation + csv + intel before ICP filtering.
        if not (state.get("val_done") and state.get("csv_done") and state.get("intel_done")):
            return "pause"
        return "dispatch_icp"

    # ICP qualification and deep research are merged into dispatch_icp
    # (find_companies_worker), which advances straight to STAGE_4_RESEARCH_COMPLETE.
    # There is no separate deep-research dispatch.
    if not state.get("ranking_done"):
        return "dispatch_rank"
    if not state.get("drafting_done"):
        return "dispatch_draft"
    return "complete"


# --------------------------------------------------------------------------- #
# Dispatch nodes (thin: set interim status + enqueue existing worker)          #
# --------------------------------------------------------------------------- #
def _set_status(campaign_id: str, status: models.CampaignStatus) -> None:
    with db_session() as db:
        campaign = (
            db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        )
        if campaign and campaign.status != status:
            campaign.status = status
            db.commit()


def _node_csv(state: CampaignWorkflowState):
    from app.workers.tasks.intel_worker import process_csv_worker
    cid = state["campaign_id"]
    process_csv_worker.delay(cid)
    logger.info(f"[WORKFLOW] -> process_csv_worker dispatched for {cid}")
    return {"dispatched": ["csv"]}


def _node_intel(state: CampaignWorkflowState):
    from app.workers.tasks.intel_worker import research_user_company_worker
    cid = state["campaign_id"]
    _set_status(cid, models.CampaignStatus.RESEARCHING_USER_COMPANY)
    research_user_company_worker.delay(cid)
    logger.info(f"[WORKFLOW] -> research_user_company_worker dispatched for {cid}")
    return {"dispatched": ["intel"]}


def _node_icp(state: CampaignWorkflowState):
    from app.workers.tasks.discovery_worker import find_companies_worker
    cid = state["campaign_id"]
    _set_status(cid, models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE)
    find_companies_worker.delay(cid)
    logger.info(f"[WORKFLOW] -> find_companies_worker (ICP) dispatched for {cid}")
    return {"dispatched": ["icp"]}


def _node_rank(state: CampaignWorkflowState):
    from app.workers.tasks.discovery_worker import find_dms_worker
    cid = state["campaign_id"]
    find_dms_worker.delay(cid)
    logger.info(f"[WORKFLOW] -> find_dms_worker dispatched for {cid}")
    return {"dispatched": ["rank"]}


def _node_draft(state: CampaignWorkflowState):
    from app.workers.tasks.ghostwriter_worker import draft_emails_worker
    cid = state["campaign_id"]
    draft_emails_worker.delay(cid)
    logger.info(f"[WORKFLOW] -> draft_emails_worker dispatched for {cid}")
    return {"dispatched": ["draft"]}


def _node_input_gate(state: CampaignWorkflowState):
    """Human-in-the-loop pause. Mirrors legacy INTERVENTION_NEEDED: the campaign
    halts here until the user edits inputs (PATCH /campaigns/{id}) which clears
    the validation review and re-fires validation, re-entering the graph."""
    cid = state["campaign_id"]
    _set_status(cid, models.CampaignStatus.INTERVENTION_NEEDED)
    logger.warning(f"[WORKFLOW] Campaign {cid} paused at input_gate (INTERVENTION_NEEDED).")
    return {"dispatched": ["input_gate"]}


def _node_pause(state: CampaignWorkflowState):
    """Barrier not yet satisfied — no-op; a later worker completion re-pokes."""
    return {"dispatched": ["pause"]}


def _node_complete(state: CampaignWorkflowState):
    """All stages done. Terminal status is owned by the drafting worker."""
    return {"dispatched": ["complete"]}


_NODES = {
    "dispatch_csv": _node_csv,
    "dispatch_intel": _node_intel,
    "dispatch_icp": _node_icp,
    "dispatch_rank": _node_rank,
    "dispatch_draft": _node_draft,
    "input_gate": _node_input_gate,
    "pause": _node_pause,
    "complete": _node_complete,
}


def build_graph(checkpointer=None):
    """Compile the campaign workflow graph. Pass a checkpointer for durability;
    None compiles an ephemeral graph used as a crash-proof fallback."""
    builder = StateGraph(CampaignWorkflowState)
    for name, fn in _NODES.items():
        builder.add_node(name, fn)
    builder.add_conditional_edges(START, route, list(_NODES.keys()))
    for name in _NODES:
        builder.add_edge(name, END)
    return builder.compile(checkpointer=checkpointer)
