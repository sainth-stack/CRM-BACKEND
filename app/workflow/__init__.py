"""
Outreach v4 — LangGraph Workflow Engine (Phase 1 spine).

This package is the explicit, checkpointed LangGraph StateGraph that is the single
source of truth for campaign stage transitions. It is a *routing brain only*:
every node is a thin dispatcher that enqueues the existing Celery worker for a
stage and updates campaign status. No agent/LLM behaviour lives here.

`CampaignService.process_state_machine` delegates to this engine
(`runner.advance_workflow`); if the checkpointer is unavailable the runner falls
back to an ephemeral graph so routing always proceeds.
"""
