import sys
import os
import argparse
from sqlalchemy import text, inspect
from alembic.config import Config
from alembic import command
from app.db.models import Base
from app.db.database import engine

def main():
    parser = argparse.ArgumentParser(description="AI-PRIORI Database Initialization & Sync Protocol")
    parser.add_argument(
        "--force-wipe",
        action="store_true",
        help="Forcefully wipe the entire database schema before initializing. WARNING: Deletes all data."
    )
    args = parser.parse_args()

    # Determine if a destructive wipe is requested (via CLI or environment flag)
    should_wipe = args.force_wipe or os.getenv("WIPE_DB_ON_INIT", "false").lower() == "true"

    if should_wipe:
        print("🚨 WARNING: Destructive wipe requested. Wiping existing database schemas...")
        try:
            with engine.connect() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE;"))
                conn.execute(text("CREATE SCHEMA public;"))
                conn.execute(text("GRANT ALL ON SCHEMA public TO public;"))
                conn.commit()
            print("✅ Schema 'public' wiped and reset successfully.")
        except Exception as e:
            print(f"⚠️ Note: Error during schema wipe (may be permissions or already clean): {e}")
    else:
        print("🔒 Mode: Safe Non-Destructive Ingestion (Ensuring tables exist without losing data)...")

    # Safe check-and-create step: creates missing tables, leaves existing tables and data untouched
    print("🛠️  Syncing table metadata...")
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ Database tables and enums synchronized successfully.")
    except Exception as e:
        print(f"❌ Critical Error creating tables: {e}")
        sys.exit(1)

    # Smart Alembic stamping: Only stamps the head if this is a freshly created database
    try:
        inspector = inspect(engine)
        has_version_table = "alembic_version" in inspector.get_table_names()
        
        if not has_version_table:
            print("📌 Fresh database detected. Stamping Alembic version to 'head'...")
            alembic_cfg = Config("alembic.ini")
            command.stamp(alembic_cfg, "head")
            print("✅ Alembic version stamped successfully.")
        else:
            print("ℹ️  Existing migration history detected. Skipping Alembic stamping to protect existing tracking.")
    except Exception as e:
        print(f"⚠️ Note: Could not evaluate or stamp Alembic version: {e}")

if __name__ == "__main__":
    main()
