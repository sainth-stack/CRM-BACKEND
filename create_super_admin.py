import sys
from app.db.database import SessionLocal
from app.db import models
from app.core.security import get_password_hash

def create_super_admin():
    db = SessionLocal()
    try:
        email = "vinay.k@ai-priori.com"
        password = "1234567890"
        
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            print(f"User {email} already exists. Updating to SUPER_ADMIN with new password.")
            user.role = "super_admin"
            user.hashed_password = get_password_hash(password)
        else:
            print(f"Creating new SUPER_ADMIN user: {email}")
            user = models.User(
                email=email,
                role="super_admin",
                hashed_password=get_password_hash(password)
            )
            db.add(user)
        
        db.commit()
        print("Super admin account created/updated successfully!")
    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    create_super_admin()
