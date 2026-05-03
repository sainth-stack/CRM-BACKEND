import sys
import os
from sqlalchemy import text
from alembic.config import Config
from alembic import command
from app.db.models import Base
from app.db.database import engine

def main():
    print("Wiping existing partial/broken tables and enums...")
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE;"))
            conn.execute(text("CREATE SCHEMA public;"))
            conn.execute(text("GRANT ALL ON SCHEMA public TO public;"))
            conn.commit()
        print("Schema public reset successfully.")
    except Exception as e:
        print(f"Note: Error during drop schema (may not be empty or insufficient permissions): {e}")

    print("Creating all tables via SQLAlchemy...")
    try:
        Base.metadata.create_all(bind=engine)
        print("All tables and enums created successfully.")
    except Exception as e:
        print(f"Error creating tables: {e}")
        return

    print("Stamping database with Alembic head...")
    try:
        alembic_cfg = Config("alembic.ini")
        command.stamp(alembic_cfg, "head")
        print("Alembic version stamped to head successfully.")
    except Exception as e:
        print(f"Note: Could not stamp alembic version directly: {e}")

if __name__ == "__main__":
    main()
