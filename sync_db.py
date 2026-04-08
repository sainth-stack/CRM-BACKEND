import subprocess
import sys
import os

def run_command(command):
    print(f"Executing: {' '.join(command)}")
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error: {e.stderr}")
        return False
    return True

def main():
    print("AI-PRIORI Database Synchronization Tool")
    print("========================================")
    
    # Ensure dependencies are available
    try:
        import alembic
        from sqlalchemy import create_engine
    except ImportError:
        print("Required libraries missing. Please run: pip install alembic sqlalchemy")
        sys.exit(1)

    # 1. Check if migrations directory is already initialized
    if not os.path.exists("migrations/versions"):
        os.makedirs("migrations/versions", exist_ok=True)
        print("Initialized brand new migration sector.")

    # 1. First, always try to bring the DB up to date with existing migrations
    print("\n[PHASE 1] Synchronizing with existing migrations...")
    run_command(["alembic", "upgrade", "head"])

    # 2. Try to generate a new migration for any pending model changes
    print("\n[PHASE 2] Analyzing current models for new changes...")
    if run_command(["alembic", "revision", "--autogenerate", "-m", "update_schema"]):
        print("SUCCESS: New migration script generated.")
        
        # 3. Apply the new migration
        print("\n[PHASE 3] Applying new schema updates...")
        if run_command(["alembic", "upgrade", "head"]):
            print("\nMISSION COMPLETE: Database is fully synchronized.")
        else:
            print("\nFAILURE: Could not apply new migrations.")
    else:
        print("\n[INFO] No new model changes detected or autogenerate skipped.")
        print("Attempting direct creation fallback for any missing tables...")
        
        from app.db.database import engine
        from app.db.models import Base
        Base.metadata.create_all(bind=engine)
        print("Sync operation complete.")

if __name__ == "__main__":
    main()
