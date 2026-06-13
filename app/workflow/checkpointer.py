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


def _is_alive(saver) -> bool:
    """Cheap pre-ping against the checkpointer's psycopg connection. Detects a
    Neon-idle-suspended / pgbouncer-recycled connection BEFORE the graph invoke
    fails, so we reconnect cleanly instead of wasting an attempt + logging an
    error. Adds ~1 round-trip per workflow advance (negligible on Neon pooled)."""
    try:
        with saver.conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


def get_checkpointer():
    """Return a process-wide PostgresSaver, or None if it cannot be created.

    Self-healing: if the cached connection is dead (Neon idle-suspend), it is
    transparently discarded and recreated on the next call — no error reaches
    the workflow runner."""
    global _saver
    if _saver is not None:
        if _is_alive(_saver):
            return _saver
        # Dead connection — drop and reconnect under the lock below.
        logger.warning("[WORKFLOW] Checkpointer connection stale (Neon idle); reconnecting.")
        try:
            _saver.conn.close()
        except Exception:
            pass
        _saver = None
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
                # TCP keepalives — same settings as the SQLAlchemy engine in
                # app/db/database.py. Prevents Neon serverless / pgbouncer from
                # killing the idle checkpointer connection between workflow stages,
                # which was the root cause of the "server closed the connection
                # unexpectedly" errors after long Stage-5 runs.
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
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
