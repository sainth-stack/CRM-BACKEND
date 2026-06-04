"""Durable LangGraph checkpointer (Neon Postgres) with a safe fallback.

The checkpointer persists graph execution history for durability / audit /
lineage. It is deliberately NOT on the correctness-critical path: routing is
re-derived from the database on every invocation, so if Postgres checkpointing
is unavailable the workflow must still advance. `runner.advance_workflow`
therefore falls back to an un-checkpointed graph on any checkpointer error.

Notes for Neon + PgBouncer:
  * autocommit=True is required by PostgresSaver.
  * prepare_threshold=None disables server-side prepared statements, which is
    mandatory when NEON_DB_URL points at the pooled (PgBouncer transaction-mode)
    endpoint.
The connection is created lazily, once per process, and reset on failure so a
dropped Neon serverless connection self-heals on the next call.
"""
from __future__ import annotations

import os
import threading

from app.core.logging_config import logger

_saver = None
_lock = threading.Lock()


def _db_url() -> str:
    from app.core.config import settings
    url = (settings.NEON_DB_URL or os.getenv("DATABASE_URL") or "").strip()
    return url.strip("'").strip('"')


def get_checkpointer():
    """Return a process-wide PostgresSaver, or None if it cannot be created."""
    global _saver
    if _saver is not None:
        return _saver
    with _lock:
        if _saver is not None:
            return _saver
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg import Connection

            conn = Connection.connect(
                _db_url(),
                autocommit=True,
                prepare_threshold=None,
            )
            saver = PostgresSaver(conn)
            saver.setup()  # idempotent: CREATE TABLE IF NOT EXISTS ...
            _saver = saver
            logger.info("[WORKFLOW] PostgresSaver checkpointer initialised.")
        except Exception as exc:  # pragma: no cover - infra dependent
            logger.error(
                f"[WORKFLOW] PostgresSaver unavailable ({exc}); "
                f"workflow will run without durable checkpoints until it recovers."
            )
            _saver = None
    return _saver


def reset_checkpointer() -> None:
    """Drop the cached saver so the next call reconnects (Neon idle-drop heal)."""
    global _saver
    with _lock:
        try:
            if _saver is not None and hasattr(_saver, "conn"):
                _saver.conn.close()
        except Exception:
            pass
        _saver = None
