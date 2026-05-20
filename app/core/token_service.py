import os
from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials
from app.db import models
from app.core.security import decrypt_token
from app.core.auth import GoogleAuthService
from app.core.logging_config import logger

class TokenService:
    """
    Vaulted Identity Anchor Service.
    Orchestrates the retrieval and decryption of secure OAuth2 credentials to enable 
    autonomous sector outreach and communication monitoring.
    """
    @staticmethod
    def get_google_credentials(db: Session, user_id: str) -> Credentials:
        """
        Credential Recovery Protocol.
        Fetches the vaulted refresh token for a user, executes cryptographic decryption, 
        and reconstructs a valid Google Credentials object for API interactions.
        """
        # 1. Fetch Capability from Vaulted Identity Store
        oauth_acc = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.user_id == user_id,
            models.OAuthAccount.provider == "google"
        ).first()

        if not oauth_acc:
            from app.core.config import settings
            if settings.GMAIL_TOKEN_JSON:
                try:
                    import json
                    data = json.loads(settings.GMAIL_TOKEN_JSON)
                    logger.info(f"[IDENTITY] No Google OAuthAccount connected for user {user_id}. Falling back to global GMAIL_TOKEN_JSON.")
                    return Credentials.from_authorized_user_info(data)
                except Exception as e:
                    logger.error(f"[IDENTITY] Failed to load global GMAIL_TOKEN_JSON fallback: {e}")
            logger.warning(f"[IDENTITY] No Google outreach capability identified for user sector {user_id} and no global fallback configured.")
            return None

        # 2. Decrypt the Anchor Token
        refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)

        from app.core.config import settings

        # 3. Synchronize Credentials Object for Autonomous Operation
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send"
            ]
        )
        
        return creds

    @staticmethod
    async def refresh_and_update_access(db: Session, user_id: str):
        """
        Identity Lifecycle Management.
        Supports proactive synchronization of access tokens when required by external integrations.
        (Note: google-auth-library implements automated JIT-refreshing by default).
        """
        pass
