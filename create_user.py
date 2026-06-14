import os
import sys
import argparse

# ==========================================
# PLACEHOLDERS
# ==========================================
# If you want to hardcode credentials, you can put them here.
# Otherwise, you can set the DATABASE_URL environment variable and just change EMAIL and PASSWORD.
DATABASE_URL = "postgresql://neondb_owner:npg_OuNo0lgAkP8J@ep-curly-math-abt4gwpu-pooler.eu-west-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"  # e.g., "postgresql://user:password@localhost/dbname"
EMAIL = "vinaykumarreddy8374@gmail.com"
PASSWORD = "1234567890"
ROLE = "user" # Options: super_admin, admin, user
# ==========================================

def main():
    # Set the DATABASE_URL environment variable if provided in the script
    if DATABASE_URL:
        os.environ["DATABASE_URL"] = DATABASE_URL
        
    # We import these here so that environment variables are set before SQLAlchemy initializes
    try:
        from app.db.database import SessionLocal
        from app.db.models.user import User, UserRole
        from app.core.security import get_password_hash
    except ImportError as e:
        print(f"Import Error: {e}")
        print("Please run this script from the root of the 'backend' directory with your virtual environment activated.")
        sys.exit(1)

    db = SessionLocal()
    try:
        existing_user = db.query(User).filter(User.email == EMAIL).first()
        hashed_password = get_password_hash(PASSWORD)
        
        # Resolve the role enum
        try:
            role_enum = UserRole(ROLE)
        except ValueError:
            print(f"Invalid role '{ROLE}'. Must be one of: {[e.value for e in UserRole]}")
            return
            
        if existing_user:
            print(f"User with email '{EMAIL}' already exists. Updating role and password...")
            existing_user.role = role_enum
            existing_user.hashed_password = hashed_password
            db.commit()
            print(f"Successfully updated user: {EMAIL} to role: {ROLE}")
            return

        hashed_password = get_password_hash(PASSWORD)
        
        # Resolve the role enum
        try:
            role_enum = UserRole(ROLE)
        except ValueError:
            print(f"Invalid role '{ROLE}'. Must be one of: {[e.value for e in UserRole]}")
            return

        new_user = User(
            email=EMAIL,
            hashed_password=hashed_password,
            role=role_enum,
            is_active=True
        )
        
        db.add(new_user)
        db.commit()
        print(f"Successfully created user: {EMAIL} with role: {ROLE}")
        
    except Exception as e:
        db.rollback()
        print(f"An error occurred while creating the user: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    main()
