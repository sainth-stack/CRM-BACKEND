import sys
import os
import uuid
from sqlalchemy.orm import Session

# Add current directory to path so we can import app
sys.path.append(os.getcwd())

from app.db.database import SessionLocal, engine
from app.db import models
from app.core.security import get_password_hash

def create_super_admin():
    email = "vinay.k@ai-priori.com"
    password = "1234567890"
    
    db = SessionLocal()
    try:
        # Check if user already exists
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            print(f"User {email} already exists. Updating to SUPER_ADMIN.")
            user.role = models.UserRole.SUPER_ADMIN
            user.hashed_password = get_password_hash(password)
        else:
            print(f"Creating new SUPER_ADMIN: {email}")
            user = models.User(
                id=str(uuid.uuid4()),
                email=email,
                hashed_password=get_password_hash(password),
                role=models.UserRole.SUPER_ADMIN,
                is_demo=False
            )
            db.add(user)
        
        db.commit()
        print("Super Admin successfully provisioned.")
    except Exception as e:
        db.rollback()
        print(f"Error provisioning super admin: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    create_super_admin()
