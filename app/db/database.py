from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
import os
from app.core.config import settings
from app.core.logging_config import logger

DATABASE_URL = settings.NEON_DB_URL or os.getenv("DATABASE_URL")
if DATABASE_URL:
    DATABASE_URL = DATABASE_URL.strip("'").strip('"')

# Enforce Neon DB / PostgreSQL Connectivity
if not DATABASE_URL:
    logger.critical("FATAL: NEON_DB_URL not detected. Database initialization aborted. Please verify environment variables.")
    raise ValueError("Database configuration missing: NEON_DB_URL must be defined.")

# Neon-safe connection configuration (managed Postgres + small instance).
# Sessions in this app are deliberately short-lived (read -> close -> AI work ->
# reopen), so a large pool is wasteful and dangerous: Neon caps total
# connections, and 60-per-process across every worker/uvicorn process would
# blow straight past that ceiling. Keep each process's footprint tiny.
#   IMPORTANT: point NEON_DB_URL at the *pooled* endpoint (host contains
#   "-pooler") so PgBouncer absorbs bursts and survives Neon auto-suspend.
# Database-specific engine and connection tuning
if DATABASE_URL.startswith("sqlite"):
    engine_args = {}
    engine_args["connect_args"] = {"check_same_thread": False}
else:
    engine_args = {
        "pool_pre_ping": True, # Verify connection liveness (essential for Neon auto-suspend)
        "pool_recycle": 300,   # Recycle well before Neon drops idle serverless connections
        "pool_size": 3,        # Small steady-state pool per process
        "max_overflow": 2,     # = 5 connections max per process under burst
        "pool_timeout": 30,    # Max wait latency for pool acquisition
    }
    # Persistence Keepalive configuration for Neon/Serverless PostgreSQL
    engine_args["connect_args"] = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5
    }

engine = create_engine(
    DATABASE_URL,
    **engine_args
)

# Automated identity-scoped session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# --------------------------------------------------------------------------- #
# Campaign cache invalidation (single chokepoint for ALL writes)              #
#                                                                             #
# The campaign-detail endpoint is cached in Redis keyed by a per-campaign     #
# generation counter. Rather than clearing the cache at every write call-site #
# (error-prone), we listen on the session: whenever a commit changes a row    #
# that feeds the campaign detail payload, we bump that campaign's generation  #
# so the next read rebuilds fresh. Works for both web and worker processes    #
# (the generation lives in shared Redis). Fully no-op if Redis is down.       #
# --------------------------------------------------------------------------- #
# Model class-names whose changes are visible in the campaign detail payload.
# (Class-name check avoids importing models here -> no circular import.)
_CACHE_RELEVANT_MODELS = {
    "Campaign", "UserCompanyIntel", "TargetCompany", "DecisionMaker",
    "EmailDraft", "CommunicationLog", "ProspectLifecycleTransition",
}


def _campaign_id_of(obj):
    cid = getattr(obj, "campaign_id", None)
    if cid:
        return cid
    if type(obj).__name__ == "Campaign":
        return getattr(obj, "id", None)
    return None


@event.listens_for(SessionLocal, "before_flush")
def _track_dirty_campaigns(session, flush_context, instances):
    import itertools
    dirty = session.info.setdefault("_dirty_campaigns", set())
    for obj in itertools.chain(session.new, session.dirty, session.deleted):
        if type(obj).__name__ in _CACHE_RELEVANT_MODELS:
            cid = _campaign_id_of(obj)
            if cid:
                dirty.add(cid)


@event.listens_for(SessionLocal, "after_commit")
def _bump_dirty_campaigns(session):
    ids = session.info.pop("_dirty_campaigns", None)
    if not ids:
        return
    try:
        from app.core.cache import bump_campaign_generation
        for cid in ids:
            bump_campaign_generation(cid)
    except Exception as e:  # never let cache bookkeeping break a commit
        logger.debug(f"[CACHE] campaign generation bump skipped: {e}")

def get_db():
    """
    Database Session Injection Dependency.
    Provides a isolated transactional boundary for the duration of a request or task lifecycle.
    Ensures deterministic cleanup and connection return to the pool.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception as e:
            logger.warning(f"[DB] Session close encountered an exception (connection likely already terminated): {e}")
