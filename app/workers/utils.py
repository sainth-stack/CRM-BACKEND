import datetime
from contextlib import contextmanager
from datetime import UTC
from sqlalchemy.orm import Session
from app.db import models
from app.core.logging_config import logger
from typing import Callable, Any, Optional
from app.db.database import SessionLocal


@contextmanager
def db_session():
    """
    Canonical context manager for short-lived DB sessions in Celery workers.
    Eliminates the manual SessionLocal() + try/finally db.close() boilerplate.

    Usage:
        with db_session() as db:
            result = db.query(models.Campaign).first()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def acquire_lease(db: Session, campaign_id: str, worker_id: str) -> bool:
    """
    Acquires a distributed lease for a specific campaign.
    Prevents concurrent execution and sets the initial heartbeat.

    Atomic compare-and-set: the claim succeeds only if the lease is free (no
    owner), already ours, or stale (no/old heartbeat). This is a single
    `UPDATE ... WHERE` so two workers racing through the Redis-lock TTL gap can
    never both win — Postgres row-locking serialises the predicate. (The previous
    read-then-write version had a TOCTOU window where both could claim.)
    """
    from sqlalchemy import or_

    now = datetime.datetime.now(UTC).replace(tzinfo=None)
    threshold = now - datetime.timedelta(minutes=10)

    updated = (
        db.query(models.Campaign)
        .filter(
            models.Campaign.id == campaign_id,
            or_(
                models.Campaign.locked_by.is_(None),
                models.Campaign.locked_by == worker_id,
                models.Campaign.last_heartbeat.is_(None),
                models.Campaign.last_heartbeat < threshold,
            ),
        )
        .update(
            {"locked_by": worker_id, "last_heartbeat": now},
            synchronize_session=False,
        )
    )
    db.commit()
    if not updated:
        logger.warning(f"[LEASE BLOCKED] Campaign {campaign_id} held by a live worker. {worker_id} aborting.")
    return bool(updated)

def heartbeat_lease(db: Session, campaign_id: str, worker_id: str):
    """
    Updates the heartbeat for an active lease to prevent the sweeper from reclaiming it.
    """
    try:
        db.query(models.Campaign).filter(
            models.Campaign.id == campaign_id,
            models.Campaign.locked_by == worker_id
        ).update({"last_heartbeat": datetime.datetime.now(UTC).replace(tzinfo=None)}, synchronize_session=False)
        db.commit()
    except Exception as e:
        logger.error(f"[HEARTBEAT FAILURE] Failed to pulse for campaign {campaign_id}: {e}")

def release_lease(db: Session, campaign_id: str, worker_id: str):
    """
    Releases a campaign lease upon successful phase completion or terminal failure.
    """
    try:
        db.query(models.Campaign).filter(
            models.Campaign.id == campaign_id,
            models.Campaign.locked_by == worker_id
        ).update({"locked_by": None, "last_heartbeat": datetime.datetime.now(UTC).replace(tzinfo=None)}, synchronize_session=False)
        db.commit()
    except Exception as e:
        logger.error(f"[LEASE RELEASE FAILURE] Failed to release lock for campaign {campaign_id}: {e}")


import inspect
from functools import wraps

def with_short_lived_db_session(func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator that provides a short-lived DB session to worker functions.
    Ensures DB sessions are properly closed after use, preventing stale sessions.

    Pattern:
    i. Read from DB
    ii. Close session
    iii. Run AI/external work
    iv. Open new session and save results

    Usage:
    @with_short_lived_db_session
    def my_worker(db: Session, campaign_id: str):
        # This function gets a short-lived session
        # When it completes, session is automatically closed
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Create a new session for this call
        db = SessionLocal()
        try:
            # Check if this is a bound Celery task (args[0] is self)
            if args and hasattr(args[0], 'request'):
                # Bound task: Inject db as the second positional argument (after self)
                return func(args[0], db, *args[1:], **kwargs)
            else:
                # Unbound function or task: Inject db as the first positional argument
                return func(db, *args, **kwargs)
        finally:
            # Always close the session
            db.close()

    # Expose the post-injection signature (without `db`) so external
    # introspectors — notably Celery's argument-typing check in apply_async,
    # which follows @wraps' __wrapped__ pointer back to `func` — validate
    # against the args we actually call with, not the injected `db` param.
    try:
        sig = inspect.signature(func)
        wrapper.__signature__ = sig.replace(
            parameters=[p for name, p in sig.parameters.items() if name != "db"]
        )
    except (ValueError, TypeError):
        pass
    return wrapper


def execute_with_db_session(func: Callable[[Session], Any]) -> Any:
    """
    Executes a function with a short-lived DB session.
    Used for cases where the function doesn't fit the decorator pattern.

    Usage:
        result = execute_with_db_session(lambda db: db.query(models.Campaign).first())
    """
    db = SessionLocal()
    try:
        return func(db)
    finally:
        db.close()
