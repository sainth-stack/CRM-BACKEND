"""Celery <-> LangGraph bridge.

`advance_workflow(campaign_id)` is the single entry point. It is invoked
synchronously from `CampaignService.process_state_machine` (the chokepoint every
stage worker and the resurrection sweeper already call). It:

  1. Reads the routing-flag snapshot from the DB (source of truth).
  2. Invokes the compiled graph for this campaign's thread, which dispatches the
     next stage's Celery worker(s) and updates interim status.

Concurrency: multiple workers can finish near-simultaneously and each call
advance_workflow for the same campaign. A short Redis lock serialises graph
invocations per campaign so concurrent checkpoint writes can't race; because
every completing worker commits its status *before* calling advance_workflow,
whichever invocation runs last re-reads the freshest flags and routes correctly.

Durability: the checkpointed graph is tried first; on ANY checkpointer error we
reset the connection and fall back to an ephemeral graph so a campaign can never
stall on infrastructure problems.
"""
from __future__ import annotations

import threading
import time

from app.core.logging_config import logger
from app.core.security import acquire_lock, release_lock
from app.workflow.checkpointer import get_checkpointer, reset_checkpointer
from app.workflow.flags import read_flags
from app.workflow.graph import build_graph

# Compiled-graph singletons (per process).
_checkpointed_graph = None
# The saver the cached checkpointed graph was built against. A compiled graph binds
# its checkpointer (and therefore its psycopg connection) at build time, so when
# get_checkpointer() heals a Neon idle-drop and hands back a *new* saver, the cached
# graph is still bound to the old, closed connection. Tracking the saver identity
# lets us rebuild the graph on reconnect instead of invoking against a dead socket
# (the root cause of "the connection is closed" after long stages).
_checkpointed_graph_saver = None
_plain_graph = None

# Serialises checkpointed-graph invocations within a process. The PostgresSaver
# holds a single psycopg connection that is NOT safe for concurrent use, and the
# local dev workers run on the THREADS pool (Windows) where multiple campaigns can
# invoke concurrently. Graph invokes are infrequent + fast, so process-wide
# serialisation here is cheap and removes the shared-connection race.
_invoke_lock = threading.Lock()


def _get_checkpointed_graph():
    global _checkpointed_graph, _checkpointed_graph_saver
    cp = get_checkpointer()
    if cp is None:
        return None
    # Rebuild when the graph is missing OR the saver was swapped out by a reconnect.
    # `is not` compares object identity: a healed connection yields a fresh saver.
    if _checkpointed_graph is None or _checkpointed_graph_saver is not cp:
        _checkpointed_graph = build_graph(checkpointer=cp)
        _checkpointed_graph_saver = cp
    return _checkpointed_graph


def _get_plain_graph():
    global _plain_graph
    if _plain_graph is None:
        _plain_graph = build_graph(checkpointer=None)
    return _plain_graph


def _acquire_route_lock(campaign_id: str) -> bool:
    """Best-effort short-spin lock; proceeds unlocked after a few tries (the
    stuck-campaign sweeper is the ultimate backstop)."""
    key = f"workflow_route:{campaign_id}"
    for _ in range(6):
        if acquire_lock(key, ttl=60):
            return True
        time.sleep(0.5)
    logger.warning(f"[WORKFLOW] Could not acquire route lock for {campaign_id}; proceeding best-effort.")
    return False


def advance_workflow(campaign_id: str) -> None:
    """Advance the campaign workflow by one routing step."""
    flags = read_flags(campaign_id)
    if flags is None:
        logger.warning(f"[WORKFLOW] advance_workflow: campaign {campaign_id} not found.")
        return

    state_input = {"campaign_id": campaign_id, **flags, "dispatched": []}
    config = {"configurable": {"thread_id": campaign_id}}

    locked = _acquire_route_lock(campaign_id)
    try:
        graph = _get_checkpointed_graph()
        if graph is not None:
            try:
                with _invoke_lock:
                    graph.invoke(state_input, config)
                return
            except Exception as exc:
                logger.error(
                    f"[WORKFLOW] Checkpointed invoke failed for {campaign_id} ({exc}); "
                    f"resetting checkpointer and retrying ephemerally.",
                    exc_info=True,
                )
                reset_checkpointer()
                global _checkpointed_graph, _checkpointed_graph_saver
                _checkpointed_graph = None
                _checkpointed_graph_saver = None

        # Fallback: routing still works without a checkpointer.
        _get_plain_graph().invoke(state_input)
    finally:
        if locked:
            release_lock(f"workflow_route:{campaign_id}")
