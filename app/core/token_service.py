import os
import json
import time
from datetime import datetime, UTC, timedelta
from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from app.db import models
from app.core.security import decrypt_token, encrypt_token
from app.core.auth import GoogleAuthService
from app.core.logging_config import logger
from app.core.config import settings
import requests

class TokenService:
    """
    Vaulted Identity Anchor Service.
    Orchestrates the retrieval and decryption of secure OAuth2 credentials to enable 
    autonomous sector outreach and communication monitoring.
    """
    @staticmethod
    def get_google_credentials(db: Session, user_id: str) -> Credentials:
        """
        Credential Recovery Protocol with Auto-Refresh.
        Fetches the vaulted refresh token for a user, executes cryptographic decryption, 
        reconstructs a valid Google Credentials object, and auto-refreshes if expired.
        """
        # 1. Fetch Capability from Vaulted Identity Store
        oauth_acc = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.user_id == user_id,
            models.OAuthAccount.provider == "google"
        ).first()

        if not oauth_acc:
            if settings.GMAIL_TOKEN_JSON:
                try:
                    data = json.loads(settings.GMAIL_TOKEN_JSON)
                    logger.info(f"[IDENTITY] No Google OAuthAccount for user {user_id}. Using global GMAIL_TOKEN_JSON fallback.")
                    return Credentials.from_authorized_user_info(data)
                except Exception as e:
                    logger.error(f"[IDENTITY] Failed to load global GMAIL_TOKEN_JSON fallback: {e}")
            logger.warning(f"[IDENTITY] No Google capability for user {user_id} and no global fallback configured.")
            return None

         # 2. Decrypt the Anchor Token
        refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)
        
        # Load and decrypt access token if it exists
        access_token = None
        if oauth_acc.access_token:
            try:
                access_token = decrypt_token(oauth_acc.access_token)
            except Exception:
                pass

        # 3. Reconstruct Credentials Object
        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            expiry=oauth_acc.token_expiry,
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send"
            ]
        )
        
        # 4. Auto-Refresh if Expired (using google-auth's built-in refresh)
        try:
            request = Request()
            if creds.expired:
                logger.info(f"[TOKEN-REFRESH] Auto-refreshing expired Google token for user {user_id}")
                creds.refresh(request)
                
                # Update stored token in DB if refresh was successful
                if creds.token:
                    oauth_acc.access_token = encrypt_token(creds.token)
                    expiry_dt = creds.expiry
                    if expiry_dt:
                        if expiry_dt.tzinfo is not None:
                            expiry_dt = expiry_dt.astimezone(UTC).replace(tzinfo=None)
                    else:
                        expiry_dt = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=3600)
                    oauth_acc.token_expiry = expiry_dt
                    db.commit()
                    logger.info(f"[TOKEN-REFRESH] Successfully refreshed token for user {user_id}")
        except RefreshError as e:
            logger.warning(f"[TOKEN-REFRESH] Refresh token for user {user_id} is invalid or revoked. Clearing credentials: {e}")
            db.delete(oauth_acc)
            try:
                db.commit()
            except Exception:
                db.rollback()
            return None
        except Exception as e:
            logger.warning(f"[TOKEN-REFRESH] Failed to auto-refresh token for user {user_id}: {e}")
            # Token will fail when actually used, allowing graceful error handling
        
        return creds

    @staticmethod
    def refresh_and_update_access(db: Session, user_id: str, provider: str = "google"):
        """
        Identity Lifecycle Management with Proactive Refresh.
        Refreshes access tokens before they expire and updates the database.
        Supports both Google and Cal.com providers.
        """
        try:
            if provider == "google":
                oauth_acc = db.query(models.OAuthAccount).filter(
                    models.OAuthAccount.user_id == user_id,
                    models.OAuthAccount.provider == "google"
                ).first()
                
                if not oauth_acc or not oauth_acc.encrypted_refresh_token:
                    logger.warning(f"[TOKEN-REFRESH] No Google OAuth account found for user {user_id}")
                    return False
                
                refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)
                
                # Refresh via Google's token endpoint
                token_response = requests.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": settings.GOOGLE_CLIENT_ID,
                        "client_secret": settings.GOOGLE_CLIENT_SECRET,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token"
                    },
                    timeout=10
                )
                
                if token_response.status_code == 200:
                    token_data = token_response.json()
                    oauth_acc.access_token = encrypt_token(token_data.get("access_token"))
                    oauth_acc.token_expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=token_data.get("expires_in", 3600))
                    db.commit()
                    logger.info(f"[TOKEN-REFRESH] Successfully refreshed Google token for user {user_id}")
                    return True
                else:
                    logger.error(f"[TOKEN-REFRESH] Failed to refresh Google token for user {user_id}: {token_response.text}")
                    # If 400 Bad Request (invalid/revoked refresh token), clear stale credentials
                    if token_response.status_code == 400:
                        logger.warning(f"[TOKEN-REFRESH] Google refresh token is invalid or revoked for user {user_id}. Clearing OAuth Account.")
                        db.delete(oauth_acc)
                        try:
                            db.commit()
                        except Exception:
                            db.rollback()
                    return False
                    
            elif provider == "cal.com":
                # Cal.com tokens don't typically expire, but we can verify they're valid
                user = db.query(models.User).filter(models.User.id == user_id).first()
                if not user or not user.cal_access_token:
                    logger.warning(f"[TOKEN-REFRESH] No Cal.com token found for user {user_id}")
                    return False
                
                # Decrypt the access token for the API request
                try:
                    decrypted_token = decrypt_token(user.cal_access_token)
                except Exception as e:
                    logger.error(f"[TOKEN-REFRESH] Failed to decrypt Cal.com access token for user {user_id}: {e}")
                    return False
                
                # Verify token is still valid by making a test call to v2/me endpoint
                cal_response = requests.get(
                    "https://api.cal.com/v2/me",
                    headers={
                        "Authorization": f"Bearer {decrypted_token}",
                        "cal-api-version": "2024-08-13"
                    },
                    timeout=10
                )
                
                if cal_response.status_code == 200:
                    logger.info(f"[TOKEN-REFRESH] Cal.com token verified for user {user_id}")
                    return True
                else:
                    logger.error(f"[TOKEN-REFRESH] Cal.com token invalid for user {user_id}: status={cal_response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"[TOKEN-REFRESH] Error refreshing {provider} token for user {user_id}: {e}")
            return False

    @staticmethod
    def get_calendar_credentials(db: Session, user_id: str) -> dict:
        """
        Retrieve and refresh Cal.com credentials for a user.
        Returns access token and metadata for API calls.
        """
        user = db.query(models.User).filter(models.User.id == user_id).first()
        
        if not user or not user.cal_access_token:
            logger.warning(f"[CALENDAR] No Cal.com credentials for user {user_id}")
            return None
        
        return {
            "access_token": user.cal_access_token,
            "username": user.cal_username,
            "event_type_id": user.cal_event_type_id,
            "timezone": user.cal_timezone or "UTC"
        }
