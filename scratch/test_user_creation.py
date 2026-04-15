from app.db.database import SessionLocal, engine
from app.db import models
from app.core.security import get_password_hash
import datetime
from datetime import UTC, timedelta
import random

def test_signup():
    db = SessionLocal()
    email = f"test_{random.randint(1000, 9999)}@test.com"
    password = "password123"
    otp = str(random.randint(100000, 999999))
    
    print(f"Creating user {email}...")
    try:
        user = models.User(
            email=email,
            hashed_password=get_password_hash(password),
            is_demo=True,
            signup_source="demo",
            otp_code=otp,
            otp_expiry=datetime.datetime.now(UTC) + timedelta(minutes=15)
        )
        db.add(user)
        db.commit()
        print("User created successfully!")
        
        # Cleanup
        db.delete(user)
        db.commit()
        print("User cleaned up.")
    except Exception as e:
        print(f"CRITICAL ERROR during User creation: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    test_signup()
