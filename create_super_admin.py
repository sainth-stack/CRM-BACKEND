"""Provision (or upgrade) the platform's SUPER_ADMIN accounts (run manually).

Idempotent: an existing user with a listed email is upgraded to SUPER_ADMIN and
its password reset; a missing one is created. Credentials are read from the
SUPER_ADMINS env var (``email:password`` pairs, comma-separated) when present,
falling back to the built-in defaults for local bootstrap.
"""
import sys
import os
import uuid

# Ensure the project root is importable when this script is run directly.
sys.path.append(os.getcwd())

from app.db.database import SessionLocal
from app.db import models
from app.core.security import get_password_hash
from app.core.logging_config import setup_logging

logger = setup_logging()

# Default bootstrap accounts; override in any environment via SUPER_ADMINS
# (format: "email1:password1,email2:password2").
_DEFAULT_ADMINS = [
    {"email": "vinay.k@ai-priori.com", "password": "1234567890"},
    {"email": "swap.m@ai-priori.com", "password": "1234567890"},
]


def _load_admins():
    raw = os.getenv("SUPER_ADMINS")
    if not raw:
        return _DEFAULT_ADMINS
    admins = []
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            email, password = pair.split(":", 1)
            admins.append({"email": email.strip(), "password": password.strip()})
    return admins or _DEFAULT_ADMINS


def create_super_admin():
    db = SessionLocal()
    try:
        for admin in _load_admins():
            email, password = admin["email"], admin["password"]
            user = db.query(models.User).filter(models.User.email == email).first()
            if user:
                logger.info(f"User {email} already exists. Updating to SUPER_ADMIN.")
                user.role = models.UserRole.SUPER_ADMIN
                user.hashed_password = get_password_hash(password)
            else:
                logger.info(f"Creating new SUPER_ADMIN: {email}")
                db.add(models.User(
                    id=str(uuid.uuid4()),
                    email=email,
                    hashed_password=get_password_hash(password),
                    role=models.UserRole.SUPER_ADMIN,
                    is_demo=False,
                ))

        db.commit()
        logger.info("Super Admin accounts successfully provisioned.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error provisioning super admin: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    create_super_admin()
