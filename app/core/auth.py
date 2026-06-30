import os
from app.core.logging_config import logger
import requests
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from fastapi import HTTPException, status
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow

# Environment setup
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Resolve FRONTEND_URL dynamically to avoid hardcoded localhost in redirects
FRONTEND_URL = os.getenv("FRONTEND_URL")
if not FRONTEND_URL:
    env_origins = os.getenv("ALLOWED_ORIGINS")
    if env_origins:
        first_origin = env_origins.split(",")[0].strip().strip('"').strip("'")
        if first_origin:
            FRONTEND_URL = first_origin
if not FRONTEND_URL:
    FRONTEND_URL = "http://localhost:5173"

GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URL")
if not GOOGLE_REDIRECT_URI:
    GOOGLE_REDIRECT_URI = f"{FRONTEND_URL}/auth/google/callback"

CAL_CLIENT_ID = os.getenv("CAL_CLIENT_ID")
CAL_CLIENT_SECRET = os.getenv("CAL_CLIENT_SECRET")

CAL_REDIRECT_URI = os.getenv("CAL_REDIRECT_URI")
if not CAL_REDIRECT_URI:
    CAL_REDIRECT_URI = f"{FRONTEND_URL}/connect-calendar"

# Extra scopes for mailbox connection
MAILBOX_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid"
]

class GoogleAuthService:
    @staticmethod
    def get_authorization_url(user_id: str, email: str = None) -> str:
        """
        Generates a secure Google OAuth2 portal URL for mailbox synchronization.
        Implements CSRF protection via a cryptographically secure state token.
        """
        from urllib.parse import urlencode
        import secrets
        from app.core.security import _get_redis
        
        # Generate a secure state token and vault it in Redis
        state = secrets.token_urlsafe(32)
        r = _get_redis()
        if r:
            # State valid for 15 minutes
            r.setex(f"oauth_state:{user_id}", 900, state)
        else:
            logger.critical("[AUTH] Redis unreachable. State verification protocols suspended.")
            raise HTTPException(status_code=503, detail="Security infrastructure unreachable.")

        config_params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(MAILBOX_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state
        }
        
        if email:
            config_params["login_hint"] = email
            
        base_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        return f"{base_endpoint}?{urlencode(config_params)}"

    @staticmethod
    def validate_state(user_id: str, incoming_state: str) -> bool:
        """
        Validates the OAuth2 state token to prevent CSRF and session injection.
        """
        from app.core.security import _get_redis
        r = _get_redis()
        if not r:
            return False
            
        stored_state = r.get(f"oauth_state:{user_id}")
        if not stored_state:
            return False
            
        # Constant time comparison is overkill for this context but secrets.compare_digest is best practice
        import secrets
        is_valid = secrets.compare_digest(stored_state.decode() if isinstance(stored_state, bytes) else stored_state, incoming_state)
        
        # Single-use: Consume the state immediately
        r.delete(f"oauth_state:{user_id}")
        return is_valid

    @staticmethod
    def verify_id_token(token: str) -> Dict[str, Any]:
        """
        Verifies a Google ID token from the frontend and returns user info.
        For use during initial Sign-up/Login.
        """
        try:
            # Note: client_id is required to prevent token substitution attacks
            idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
            if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
                raise ValueError('Wrong issuer.')
            return idinfo
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid Google ID token: {str(e)}"
            )

    @staticmethod
    def verify_auth_code_for_mailbox(code: str, redirect_uri: str = None) -> Dict[str, Any]:
        """
        Exchanges an OAuth code for a REFRESH TOKEN. 
        For use during the "Connect Mailbox" flow.
        """
        try:
            # Standardize on the environment-configured redirect URI
            target_redirect = GOOGLE_REDIRECT_URI
            
            # Initialize the flow
            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                },
                scopes=MAILBOX_SCOPES,
                redirect_uri=target_redirect
            )
            
            # Exchange the authorization code for tokens
            flow.fetch_token(code=code)
            credentials = flow.credentials
            
            # The refresh token is only emitted if access_type was "offline" 
            # and it's the first time or prompt=consent was used.
            if not credentials.refresh_token:
                logger.warning(f"[AUTH] Critical Intelligence Gap: Refresh token missing for {target_redirect}. Long-term accessibility limited.")

            user_info_response = requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {credentials.token}"},
                timeout=10
            )
            user_info_response.raise_for_status()
            user_info = user_info_response.json()

            return {
                "refresh_token": credentials.refresh_token,
                "email": user_info.get("email"),
                "access_token": credentials.token # Temporary access
            }
        except Exception as e:
             logger.error(f"[REJECTED] Mailbox Connection Exchange Failed: {e}", exc_info=True)
             raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to exchange mailbox access code: {str(e)}"
            )

    @staticmethod
    def get_fresh_access_token(refresh_token: str) -> str:
        """
        Uses a refresh token to fetch a new short-lived access token.
        Critical for the TokenService during outbound campaign execution.
        """
        try:
             response = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=10
            )
             tokens = response.json()
             return tokens.get("access_token")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Failed to refresh mailbox accessibility: {str(e)}"
            )

class MicrosoftAuthService:
    """Design placeholder for future provider-agnostic expansion."""
    async def verify_id_token(self, token: str):
        raise NotImplementedError("Microsoft Auth engagement protocol pending mobilization.")


def _resolve_cal_expiry(data: Dict[str, Any]) -> Optional[str]:
    """Resolve the access-token expiry from a Cal.com v2 token response.

    Verified against the live API (2026-06-29): Cal.com's real response shape
    is {"access_token", "refresh_token", "expires_in": 1800, "scope"} - a
    RELATIVE duration in seconds. It does NOT send an absolute "expiresAt" /
    "expires_at" timestamp, despite that being the field this code used to
    look for exclusively (which meant the lookup always returned None and
    silently fell through to a hardcoded 1800s guess in cal.py - coincidentally
    correct only because Cal.com's real TTL also happens to be 1800s).

    Check the relative field first since that's what Cal.com actually sends;
    keep the absolute-timestamp check as a fallback in case a future API
    version switches shape.

    Returns a NAIVE (no tzinfo) ISO string in local time deliberately, NOT UTC:
    every consumer of "expires_at" (cal.py, and the connect-mailbox endpoint in
    this file) does `dateutil.parser.parse(...).replace(tzinfo=None)` and then
    compares the result against bare `datetime.now()` (also naive-local). If
    this returned a UTC-aware timestamp, `.replace(tzinfo=None)` would silently
    drop the UTC offset instead of converting it, making the stored expiry
    wrong by the local UTC offset (e.g. IST: off by 5.5 hours). Staying naive
    here keeps the round trip correct under the codebase's existing convention.
    """
    expires_in = data.get("expiresIn") if data.get("expiresIn") is not None else data.get("expires_in")
    if expires_in is not None:
        try:
            return (datetime.now() + timedelta(seconds=int(expires_in))).isoformat()
        except (TypeError, ValueError):
            pass
    return data.get("expiresAt") or data.get("expires_at")


class CalAuthService:
    @staticmethod
    def get_authorization_url(user_id: str, email: str = None) -> str:
        """
        Generates a secure Cal.com OAuth2 portal URL for calendar synchronization.
        Implements CSRF protection via a secure state token.
        """
        from urllib.parse import urlencode
        import secrets
        from app.core.security import _get_redis
        
        state = secrets.token_urlsafe(32)
        r = _get_redis()
        if r:
            r.setex(f"cal_oauth_state:{user_id}", 900, state)
        else:
            logger.critical("[AUTH] Redis unreachable. Cal state verification suspended.")
            raise HTTPException(status_code=503, detail="Security infrastructure unreachable.")

        # prompt=login is the OIDC-standard way to force the provider to re-show its
        # login screen and ignore any cached browser session. Cal.com silently ignores
        # the gentler prompt=select_account when its cookie is set, which caused users
        # to be silently authorized as the wrong Cal.com account and rejected by our
        # email-match check. login_hint prefills the email field so the user only has
        # to enter their password — keeping the happy path one click away.
        config_params = {
            "client_id": CAL_CLIENT_ID,
            "redirect_uri": CAL_REDIRECT_URI,
            "response_type": "code",
            "scope": "BOOKING_READ BOOKING_WRITE PROFILE_READ EVENT_TYPE_READ SCHEDULE_READ",
            "state": state,
            "prompt": "login",
        }

        if email:
            config_params["login_hint"] = email
        
        base_endpoint = "https://app.cal.com/auth/oauth2/authorize"
        return f"{base_endpoint}?{urlencode(config_params)}"

    @staticmethod
    def validate_state(user_id: str, incoming_state: str) -> bool:
        """
        Validates the Cal.com OAuth2 state token to prevent CSRF.
        """
        from app.core.security import _get_redis
        r = _get_redis()
        if not r:
            return False
            
        stored_state = r.get(f"cal_oauth_state:{user_id}")
        if not stored_state:
            return False
            
        import secrets
        is_valid = secrets.compare_digest(stored_state.decode() if isinstance(stored_state, bytes) else stored_state, incoming_state)
        r.delete(f"cal_oauth_state:{user_id}")
        return is_valid

    @staticmethod
    def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
        """
        Exchanges a Cal.com OAuth code for permanent access/refresh tokens.
        """
        try:
            logger.info("[AUTH-CAL] Exchanging auth code for tokens...")
            response = requests.post(
                "https://api.cal.com/v2/auth/oauth2/token",
                headers={
                    "Content-Type": "application/json",
                    "cal-api-version": "2024-08-13"
                },
                json={
                    "client_id": CAL_CLIENT_ID,
                    "client_secret": CAL_CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CAL_REDIRECT_URI
                },
                timeout=15
            )
            if not response.ok:
                logger.error(
                    f"[AUTH-CAL] Token exchange rejected by Cal.com: "
                    f"status={response.status_code} body={response.text}"
                )
            response.raise_for_status()
            body = response.json()
            # Verified live (2026-06-29): Cal.com v2 returns the token pair flat,
            # e.g. {"access_token", "refresh_token", "expires_in": 1800, "scope"} -
            # not nested under "data" and not an absolute "expiresAt" timestamp.
            # Keep the data.get("data", body) unwrap for resilience to either shape.
            data = body.get("data", body) if isinstance(body, dict) else {}
            access_token = data.get("accessToken") or data.get("access_token")
            refresh_token = data.get("refreshToken") or data.get("refresh_token")
            expires_at = _resolve_cal_expiry(data)

            return {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at
            }
        except Exception as e:
            logger.error(f"[AUTH-CAL] Token exchange failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cal.com OAuth exchange failed: {str(e)}"
            )

    @staticmethod
    def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
        """
        Refreshes an expired Cal.com access token using a refresh token.
        """
        try:
            response = requests.post(
                "https://api.cal.com/v2/auth/oauth2/token",
                headers={
                    "Content-Type": "application/json",
                    "cal-api-version": "2024-08-13"
                },
                json={
                    "client_id": CAL_CLIENT_ID,
                    "client_secret": CAL_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token
                },
                timeout=15
            )
            if not response.ok:
                logger.error(
                    f"[AUTH-CAL] Token refresh rejected by Cal.com: "
                    f"status={response.status_code} body={response.text}"
                )
            response.raise_for_status()
            body = response.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            access_token = data.get("accessToken") or data.get("access_token")
            new_refresh_token = data.get("refreshToken") or data.get("refresh_token") or refresh_token
            expires_at = _resolve_cal_expiry(data)

            return {
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "expires_at": expires_at
            }
        except Exception as e:
            logger.error(f"[AUTH-CAL] Token refresh failed: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Failed to refresh Cal.com access: {str(e)}"
            )

    @staticmethod
    def get_user_email(access_token: str) -> str:
        """
        Fetches the authenticated user's email from Cal.com API v2 to prevent identity mismatch.
        """
        try:
            response = requests.get(
                "https://api.cal.com/v2/me",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "cal-api-version": "2024-08-13"
                },
                timeout=15
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data", {})
            return data.get("email")
        except Exception as e:
            logger.error(f"[AUTH-CAL] Failed to fetch Cal.com user email: {e}")
            return None
