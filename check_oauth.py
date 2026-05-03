import os
from app.db.database import SessionLocal
from app.db import models
from app.core.security import decrypt_token
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import httplib2

db = SessionLocal()
admin = db.query(models.User).filter(models.User.email == "vinay.k@ai-priori.com").first()
if admin:
    print(f"Admin found: {admin.email}")
    oauth = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == admin.id).first()
    if oauth:
        print(f"OAuth Account: {oauth.email_address}")
        # Let's test decrypting the token
        try:
            refresh_token = decrypt_token(oauth.encrypted_refresh_token)
            print(f"Token decrypted successfully.")
            
            # Let's see if we can build the service
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=os.getenv("GOOGLE_CLIENT_ID"),
                client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
                scopes=[
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.send"
                ]
            )
            http = httplib2.Http(timeout=10)
            service = build('gmail', 'v1', credentials=creds, http=http)
            print("Successfully built Gmail service client.")
        except Exception as e:
            print(f"Error while validating OAuth or decrypting: {e}")
    else:
        print("No OAuth account.")
else:
    print("No Super Admin.")
db.close()
