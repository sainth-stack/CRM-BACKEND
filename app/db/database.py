from sqlalchemy import create_engine
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

# High-concurrency connection configuration (Sized for distributed worker clusters)
engine_args = {
    "pool_pre_ping": True, # Connection integrity verification
    "pool_recycle": 1800,  # Periodic connection recycling for serverless longevity
    "pool_size": 20,       # Increased capacity for parallel agentic processes
    "max_overflow": 40,    # Enhanced dynamic overflow for transactional bursts
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
