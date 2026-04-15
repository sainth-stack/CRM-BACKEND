import subprocess
import sys
import os
import shutil

def run_command(command, cwd=None):
    print(f"Executing: {' '.join(command)}")
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, cwd=cwd)
        print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error: {e.stderr}")
        return False

def main():
    print("AI-PRIORI Database Synchronization Tool (Fresh Start Mode)")
    print("==========================================================")
    
    # 1. Clear existing migrations to start fresh
    versions_dir = "migrations/versions"
    if os.path.exists(versions_dir):
        print(f"Clearing migration versions in {versions_dir}...")
        for filename in os.listdir(versions_dir):
            file_path = os.path.join(versions_dir, filename)
            if os.path.isfile(file_path) and filename.endswith(".py"):
                os.remove(file_path)
    else:
        os.makedirs(versions_dir, exist_ok=True)

    # 2. Ensure SQLAlchemy and Alembic are ready
    from app.db.database import engine
    from app.db.models import Base
    
    # 3. Drop all tables to ensure no conflicts (Clean Slate)
    print("\n[PHASE 1] Purging existing schema (Clean Slate)...")
    Base.metadata.drop_all(bind=engine)
    print("Direct schema purge complete.")

    # 4. Generate fresh initial migration
    print("\n[PHASE 2] Generating fresh initial migration...")
    # We might need to stamp head first if alembic_version table exists but is empty/wrong
    # But for a fresh DB, Base.metadata.drop_all should have handled it.
    
    if run_command(["alembic", "revision", "--autogenerate", "-m", "initial_schema"]):
        print("SUCCESS: Initial migration sequence generated.")
        
        # 5. Apply the new migration
        print("\n[PHASE 3] Mobilizing schema deployment...")
        if run_command(["alembic", "upgrade", "head"]):
            print("\nMISSION COMPLETE: Database is fully synchronized with deep-schema integrity.")
        else:
            print("\nFAILURE: Could not apply initial migration.")
    else:
        print("\n[FALLBACK] Autogenerate failed. Attempting direct creation...")
        Base.metadata.create_all(bind=engine)
        print("Fallback synchronization complete.")

if __name__ == "__main__":
    main()

