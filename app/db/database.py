from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("NEON_DB_URL") or os.getenv("DATABASE_URL")
if DATABASE_URL:
    DATABASE_URL = DATABASE_URL.strip("'").strip('"')

# Handle missing DB URL for tests/initialization
if not DATABASE_URL:
    print("Warning: NEON_DB_URL not found. Using local SQLite for runtime initialization.")
    DATABASE_URL = "sqlite:///./fallback.db"

engine_args = {
    "pool_pre_ping": True, # Validate connection health before use
    "pool_recycle": 1800,    # Recycle connections (Neon recommended ~30 mins)
    "pool_size": 10,        # Increase pool size for parallel agent demands
    "max_overflow": 20,     # Allow temporary spikes in connection usage
    "pool_timeout": 30,     # Wait up to 30 seconds for a connection
}

if DATABASE_URL.startswith("postgresql"):
    # Specific args to keep Neon serverless connections alive
    # We use keepalives to prevent "SSL connection has been closed unexpectedly"
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
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
