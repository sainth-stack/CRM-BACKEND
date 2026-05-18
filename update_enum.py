from app.db.database import SessionLocal
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DB-Enum-Update")

def update_enums():
    db = SessionLocal()
    new_statuses = [
        'STAGE_1_CSV_TRIMMED',
        'STAGE_2_USER_INTEL_COMPLETE',
        'STAGE_3_ICP_FILTERED',
        'STAGE_4_RESEARCH_COMPLETE',
        'STAGE_5_STAKEHOLDERS_RANKED',
        'STAGE_6_DRAFTING_COMPLETE',
        'PARTIAL_SUCCESS',
        'INTERVENTION_NEEDED',
        'FAILED',
        'INACTIVE'
    ]
    
    logger.info("🚀 Mobilizing Database Enum Synchronization...")
    
    for status in new_statuses:
        try:
            # Postgres specific: ADD VALUE IF NOT EXISTS
            db.execute(text(f"ALTER TYPE campaignstatus ADD VALUE IF NOT EXISTS '{status}'"))
            db.commit()
            logger.info(f"✅ Synced status: {status}")
        except Exception as e:
            db.rollback()
            if "already exists" in str(e).lower():
                logger.info(f"ℹ️ Status already exists: {status}")
            else:
                logger.warning(f"⚠️ Could not sync {status}: {e}")

    # Also update ProspectState for safety
    prospect_states = ['POSITIVE', 'NEGATIVE', 'NEUTRAL', 'DISCOVERY_CALL', 'MEETING_BOOKED']
    for state in prospect_states:
        try:
            db.execute(text(f"ALTER TYPE prospectstate ADD VALUE IF NOT EXISTS '{state}'"))
            db.commit()
        except Exception:
            db.rollback()

    db.close()
    logger.info("🏁 Database Enums are now synchronized with V2 State-Machine.")

if __name__ == "__main__":
    update_enums()
