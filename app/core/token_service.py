from datetime import datetime, UTC, timedelta
from sqlalchemy.orm import Session
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from app.db import models
from app.core.security import decrypt_token, encrypt_token
from app.core.logging_config import logger
from app.core.config import settings
import requests

class TokenService:
    """Retrieve and decrypt stored OAuth2 credentials so the app can send mail and
    monitor replies on a user's behalf."""
    @staticmethod
    def get_google_credentials(db: Session, user_id: str) -> Credentials:
        """Load, proactively refresh if near-expiry, and return Google credentials.

        Mirrors cal.py's get_valid_access_token() pattern:
        - No fallback to any global token — each user must have their own OAuthAccount.
        - Refreshes the access token synchronously when it is expired or within 120s of expiry.
        - Uses a DB row lock to prevent concurrent refresh races (two tabs clicking send).
        - If the refresh token is revoked, deletes the OAuthAccount and returns None so
          the caller can surface a 'reconnect mailbox' error rather than a silent failure.
        """
        oauth_acc = db.query(models.OAuthAccount).filter(
            models.OAuthAccount.user_id == user_id,
            models.OAuthAccount.provider == "google"
        ).first()

        if not oauth_acc:
            logger.warning(f"[GMAIL] No connected mailbox for user {user_id}.")
            return None

        now = datetime.now(UTC).replace(tzinfo=None)
        is_expired = (
            oauth_acc.token_expiry is None
            or oauth_acc.token_expiry <= now + timedelta(seconds=120)
        )

        if not is_expired:
            # Access token is still valid — build and return credentials directly.
            refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)
            access_token = None
            if oauth_acc.access_token:
                try:
                    access_token = decrypt_token(oauth_acc.access_token)
                except Exception:
                    pass
            return Credentials(
                token=access_token,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET,
                expiry=oauth_acc.token_expiry,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
            )

        # Access token expired or expiring soon — acquire row lock then re-check
        # (another concurrent caller may have already refreshed it).
        bind = db.get_bind()
        if bind is not None and bind.dialect.name != "sqlite":
            oauth_acc = (
                db.query(models.OAuthAccount)
                .filter(
                    models.OAuthAccount.user_id == user_id,
                    models.OAuthAccount.provider == "google",
                )
                .with_for_update()
                .first()
            )
            if not oauth_acc:
                return None

            still_expired = (
                oauth_acc.token_expiry is None
                or oauth_acc.token_expiry <= datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=120)
            )
            if not still_expired:
                logger.info(f"[GMAIL] Token for user {user_id} already refreshed by another process. Reusing.")
                refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)
                access_token = decrypt_token(oauth_acc.access_token) if oauth_acc.access_token else None
                return Credentials(
                    token=access_token,
                    refresh_token=refresh_token,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=settings.GOOGLE_CLIENT_ID,
                    client_secret=settings.GOOGLE_CLIENT_SECRET,
                    expiry=oauth_acc.token_expiry,
                    scopes=["https://www.googleapis.com/auth/gmail.modify"],
                )

        logger.info(f"[GMAIL] Access token expired for user {user_id}. Refreshing via Google...")
        try:
            refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)
            response = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )
            if not response.ok:
                raise RefreshError(f"Google token refresh rejected: {response.status_code} {response.text}")

            token_data = response.json()
            new_access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            new_expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=expires_in)

            oauth_acc.access_token = encrypt_token(new_access_token)
            oauth_acc.token_expiry = new_expiry
            oauth_acc.mailbox_health_status = "HEALTHY"
            db.commit()
            logger.info(f"[GMAIL] Token refreshed successfully for user {user_id}. Expires at {new_expiry}.")

            return Credentials(
                token=new_access_token,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=settings.GOOGLE_CLIENT_ID,
                client_secret=settings.GOOGLE_CLIENT_SECRET,
                expiry=new_expiry,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
            )

        except RefreshError as e:
            # Refresh token is revoked or invalid — user must reconnect.
            logger.warning(f"[GMAIL] Refresh token revoked for user {user_id}. Clearing mailbox credentials: {e}")
            db.delete(oauth_acc)
            try:
                db.commit()
            except Exception:
                db.rollback()
            return None
        except Exception as e:
            # Transient failure (network timeout, Google 5xx) — do NOT clear credentials.
            # Raise so the caller surfaces a 503 retry error rather than a false
            # "mailbox not connected" that would wrongly redirect the user to reconnect.
            logger.error(f"[GMAIL] Transient failure refreshing token for user {user_id}: {e}", exc_info=True)
            raise

    @staticmethod
    def refresh_and_update_access(db: Session, user_id: str, provider: str = "google"):
        """Refresh a provider's access token before it expires and persist it.
        Supports both Google and Cal.com."""
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
                # Proactively REFRESH the Cal.com access token, not just ping it.
                # get_valid_access_token() is the same path the booking flow uses:
                # it refreshes synchronously when the access token is expiring (via
                # the stored refresh token), and only clears credentials when the
                # refresh token itself is revoked/invalid. So a False return here
                # now means a GENUINE reconnect is required — not a misleading
                # liveness failure on a merely-stale access token.
                user = db.query(models.User).filter(models.User.id == user_id).first()
                if not user or not user.cal_refresh_token:
                    logger.warning(f"[TOKEN-REFRESH] No Cal.com connection for user {user_id}")
                    return False

                from app.integrations.cal import cal_provider
                token = cal_provider.get_valid_access_token(db, user)
                if token:
                    logger.info(f"[TOKEN-REFRESH] Cal.com token valid/refreshed for user {user_id}")
                    return True
                else:
                    logger.error(f"[TOKEN-REFRESH] Cal.com token unrecoverable for user {user_id}; reconnect required")
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
