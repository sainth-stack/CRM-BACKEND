"""
Outreach v4 — LangGraph Workflow Engine (Phase 1 spine).

This package introduces an explicit, checkpointed LangGraph StateGraph as the
single source of truth for campaign stage transitions. In Phase 1 it is a
*routing brain only*: every node is a thin dispatcher that enqueues the existing
Celery worker for a stage and updates campaign status exactly as the legacy
`CampaignService.process_state_machine` did. No agent/LLM behaviour changes here.

Activation is controlled by the WORKFLOW_ENGINE env var ("langgraph" default,
"legacy" to fall back to the original inline state machine).
"""
