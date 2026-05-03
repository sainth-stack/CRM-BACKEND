from app.db.database import SessionLocal
from sqlalchemy import text

db = SessionLocal()
try:
    # Let's add all the missing values to the campaignstatus enum in the DB
    db.execute(text("ALTER TYPE campaignstatus ADD VALUE IF NOT EXISTS 'PARTIAL_SUCCESS'"))
    print("Added PARTIAL_SUCCESS to campaignstatus")
except Exception as e:
    print(f"Error adding PARTIAL_SUCCESS: {e}")
    db.rollback()

try:
    db.execute(text("ALTER TYPE campaignstatus ADD VALUE IF NOT EXISTS 'INTERVENTION_NEEDED'"))
    print("Added INTERVENTION_NEEDED to campaignstatus")
except Exception as e:
    print(f"Error adding INTERVENTION_NEEDED: {e}")
    db.rollback()

try:
    db.execute(text("ALTER TYPE campaignstatus ADD VALUE IF NOT EXISTS 'FAILED'"))
    print("Added FAILED to campaignstatus")
except Exception as e:
    print(f"Error adding FAILED: {e}")
    db.rollback()

try:
    db.execute(text("ALTER TYPE campaignstatus ADD VALUE IF NOT EXISTS 'INACTIVE'"))
    print("Added INACTIVE to campaignstatus")
except Exception as e:
    print(f"Error adding INACTIVE: {e}")
    db.rollback()

try:
    # Also check if any other enums need new values
    db.execute(text("ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS 'POSITIVE'"))
    print("Added POSITIVE to prospectstate")
except Exception as e:
    print(f"Error adding POSITIVE: {e}")
    db.rollback()

db.commit()
db.close()
