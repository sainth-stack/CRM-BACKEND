import os
from app.core.logging_config import logger
import requests
from typing import Dict, Any, Optional
from fastapi import HTTPException, status
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow

# Environment setup
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URL", "http://localhost:5173/auth/google/callback")
# Extra scopes for mailbox connection
MAILBOX_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid"
]

class GoogleAuthService:
    @staticmethod
    def get_authorization_url(email: str = None) -> str:
        """
        Generates a secure Google OAuth2 portal URL for mailbox synchronization.
        Uses manual construction to ensure compatibility with stateless entries
        and to avoid PKCE conflicts during the cross-component handshake.
        """
        from urllib.parse import urlencode
        
        config_params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(MAILBOX_SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent"
        }
        
        if email:
            config_params["login_hint"] = email
            
        base_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
        return f"{base_endpoint}?{urlencode(config_params)}"

    @staticmethod
    async def verify_id_token(token: str) -> Dict[str, Any]:
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
    async def verify_auth_code_for_mailbox(code: str, redirect_uri: str = None) -> Dict[str, Any]:
        """
        Exchanges an OAuth code for a REFRESH TOKEN. 
        For use during the "Connect Mailbox" flow.
        """
        try:
            # Note: The redirect_uri must match exactly what was sent to Google originally.
            # Manual Redirect (Barrier) uses GOOGLE_REDIRECT_URI.
            # Popup Hook (Button) uses 'postmessage'.
            target_redirect = redirect_uri or GOOGLE_REDIRECT_URI
            
            # Initialize the flow
            flow = Flow.from_client_config(
                {
                    "web": {
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://accounts.google.com/o/oauth2/token",
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
                headers={"Authorization": f"Bearer {credentials.token}"}
            )
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
    async def get_fresh_access_token(refresh_token: str) -> str:
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
