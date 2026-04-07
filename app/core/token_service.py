import os
from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials
from app.db import models
from app.core.security import decrypt_token
from app.core.auth import GoogleAuthService

class TokenService:
    @staticmethod
    def get_google_credentials(db: Session, user_id: str) -> Credentials:
        """
        Fetches the vaulted refresh token for a user, decrypts it, 
        and returns a Google Credentials object.
        """
        # 1. Fetch Capability from Vault
        oauth_acc = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.user_id == user_id,
            models.OAuthAccount.provider == "google"
        ).first()

        if not oauth_acc:
            return None

        # 2. Decrypt Refresh Token
        refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)

        # 3. Build Credentials Object
        # Note: token=None because we want it to be refreshed on first use
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
        
        return creds

    @staticmethod
    async def refresh_and_update_access(db: Session, user_id: str):
        """
        Optional: Proactively refreshes the token.
        (google-auth library handles this automatically if refresh_token is present).
        """
        pass
