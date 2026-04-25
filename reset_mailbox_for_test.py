import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.db.database import SessionLocal
from app.db import models

def reset_mailbox():
    email = "vinaykumarreddy8374@gmail.com"
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            oauth = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == user.id).first()
            if oauth:
                db.delete(oauth)
                db.commit()
                print(f"SUCCESS: OAuth record for {email} deleted. Ready for clean test.")
            else:
                print(f"INFO: No OAuth record found for {email}.")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    reset_mailbox()
