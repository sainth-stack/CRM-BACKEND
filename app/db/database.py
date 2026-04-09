from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv
from app.core.logging_config import logger

load_dotenv()

DATABASE_URL = os.getenv("NEON_DB_URL") or os.getenv("DATABASE_URL")
if DATABASE_URL:
    DATABASE_URL = DATABASE_URL.strip("'").strip('"')

# Global persistence anchoring
if not DATABASE_URL:
    logger.warning("NEON_DB_URL not found. Mobilizing local SQLite fallback for runtime initialization.")
    DATABASE_URL = "sqlite:///./fallback.db"

# High-concurrency connection configuration
engine_args = {
    "pool_pre_ping": True, # Connection integrity verification
    "pool_recycle": 1800,  # Periodic connection recycling for serverless longevity
    "pool_size": 10,       # Concurrent pool capacity for parallel agentic processes
    "max_overflow": 20,    # Dynamic overflow capacity for transactional bursts
    "pool_timeout": 30,    # Max wait latency for pool acquisition
}

if DATABASE_URL.startswith("postgresql"):
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
        db.close()
