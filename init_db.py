"""Database initialization & sync utility (run manually, not in request flow).

Creates any missing tables from the SQLAlchemy metadata and, on a fresh database,
stamps Alembic to ``head`` so future migrations apply cleanly. Pass ``--force-wipe``
(or set ``WIPE_DB_ON_INIT=true``) to drop and recreate the ``public`` schema first.

Output is routed through the app logger rather than ``print`` so it is timestamped
and consistent with the rest of the system.
"""
import sys
import os
import argparse
from sqlalchemy import text, inspect
from alembic.config import Config
from alembic import command
from app.db.models import Base
from app.db.database import engine
from app.core.logging_config import setup_logging

logger = setup_logging()


def main():
    parser = argparse.ArgumentParser(description="AI-PRIORI Database Initialization & Sync Protocol")
    parser.add_argument(
        "--force-wipe",
        action="store_true",
        help="Forcefully wipe the entire database schema before initializing. WARNING: Deletes all data."
    )
    args = parser.parse_args()

    # Destructive wipe may be requested via CLI flag or environment variable.
    should_wipe = args.force_wipe or os.getenv("WIPE_DB_ON_INIT", "false").lower() == "true"

    if should_wipe:
        logger.warning("Destructive wipe requested. Wiping existing database schemas...")
        try:
            with engine.connect() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE;"))
                conn.execute(text("CREATE SCHEMA public;"))
                conn.execute(text("GRANT ALL ON SCHEMA public TO public;"))
                conn.commit()
            logger.info("Schema 'public' wiped and reset successfully.")
        except Exception as e:
            logger.warning(f"Error during schema wipe (may be permissions or already clean): {e}")
    else:
        logger.info("Safe non-destructive mode: ensuring tables exist without losing data...")

    # Idempotent: creates missing tables, leaves existing tables and their data untouched.
    logger.info("Syncing table metadata...")
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables and enums synchronized successfully.")
    except Exception as e:
        logger.error(f"Critical error creating tables: {e}")
        sys.exit(1)

    # Only stamp Alembic on a brand-new database; never touch an existing migration history.
    try:
        inspector = inspect(engine)
        has_version_table = "alembic_version" in inspector.get_table_names()

        if not has_version_table:
            logger.info("Fresh database detected. Stamping Alembic version to 'head'...")
            alembic_cfg = Config("alembic.ini")
            command.stamp(alembic_cfg, "head")
            logger.info("Alembic version stamped successfully.")
        else:
            logger.info("Existing migration history detected. Skipping Alembic stamping.")
    except Exception as e:
        logger.warning(f"Could not evaluate or stamp Alembic version: {e}")


if __name__ == "__main__":
    main()
