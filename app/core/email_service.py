import os
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import httplib2
from typing import Any
from app.core.logging_config import logger
from app.core.retry_utils import with_retries
from googleapiclient.errors import HttpError

class EmailService:
    """
    Service layer for GMail API interactions including campaign outreach, 
    OTP verification, and account setup.
    """
    def __init__(self):
        # Operational Efficiency: Memoize GMail service objects to avoid redundant discovery parsing
        self._service_cache = {}
    
    def send_email(self, to_email: str, subject: str, body: str, creds: Credentials, thread_id: str = None) -> dict:
        """Dispatches outreach emails through the user's connected mailbox."""
        if not creds:
            logger.error(f"Email failed: No credentials for {to_email}")
            raise Exception("No mailbox credentials identified.")
        
        try:
            return self._send_via_gmail(to_email, subject, body, creds, thread_id)
        except Exception as e:
            logger.error(f"[EMAIL] Outreach Dispatch Failure to {to_email}: {e}", exc_info=True)
            raise e

    def _get_service(self, creds: Credentials) -> Any:
        """ Identity-Anchored Service Provisioning with Discovery Caching. """
        # Identify the session by its refresh_token or temporary access token
        cache_key = hash(creds.refresh_token or creds.token)
        
        if cache_key in self._service_cache:
            return self._service_cache[cache_key]
        
        # Build new service with explicit 10s boundary
        service = build('gmail', 'v1', credentials=creds)
        self._service_cache[cache_key] = service
        return service

    @with_retries(max_attempts=3, base_delay=3.0, exceptions=(HttpError, httplib2.ServerNotFoundError, ConnectionError))
    def _send_via_gmail(self, to_email: str, subject: str, body: str, creds: Credentials, thread_id: str = None) -> dict:
        """Official Google API Bridge for high-fidelity outreach and threading with a 10s timeout guard."""
        service = self._get_service(creds)
        
        message = MIMEMultipart()
        message['To'] = to_email
        message['Subject'] = subject
        
        # High-Fidelity Threading
        if thread_id:
            message['In-Reply-To'] = thread_id
            message['References'] = thread_id

        is_html = any(tag in body.lower() for tag in ["<html", "<div", "<p", "<table", "<body"])
        
        if not is_html:
            # Fluid HTML conversion to prevent automatic hard-wrapping at 70 characters
            paragraphs = body.split('\n\n')
            html_paragraphs = []
            for p in paragraphs:
                p_clean = p.strip()
                if not p_clean:
                    continue
                p_html = p_clean.replace('\n', '<br/>')
                html_paragraphs.append(
                    f'<p style="margin: 0 0 1.2em 0; line-height: 1.5; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif; font-size: 15px; color: #334155;">{p_html}</p>'
                )
            body = "<html><body style='margin:0;padding:0;background-color:#ffffff;'>" + "".join(html_paragraphs) + "</body></html>"
            is_html = True
            
        if "<html>" not in body.lower():
            body = f"<html><body style='margin:0;padding:0;'>{body}</body></html>"
        msg_type = MIMEText(body, 'html')
            
        message.attach(msg_type)
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        data = {'raw': raw_message}
        if thread_id:
            data['threadId'] = thread_id
            
        try:
            sent_msg = service.users().messages().send(userId='me', body=data).execute()
            msg_id = sent_msg.get('id')
            new_thread_id = sent_msg.get('threadId')
            logger.info(f"[GMAIL] Sent: {to_email} (ID: {msg_id})")
            return {"id": msg_id, "thread_id": new_thread_id}
        except Exception as e:
            logger.error(f"[GMAIL] Failed to send to {to_email}: {e}")
            raise e

    def send_verification_email(self, to_email: str, otp: str):
        """
        Identity Verification Dispatch.
        Dispatches a system-critical verification code (OTP) via the core vault credentials 
        sourced from environmental synchronization (GMAIL_TOKEN_JSON).
        """
        import json
        from app.core.config import settings
        token_json = settings.GMAIL_TOKEN_JSON
        if not token_json:
            logger.error("Identity Infrastructure Failure: System vault (GMAIL_TOKEN_JSON) not identified in environment.")
            raise Exception("Identity Infrastructure Failure: System vault (GMAIL_TOKEN_JSON) not identified.")
        
        try:
            data = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(data)
        except Exception as e:
            logger.error(f"Identity Infrastructure Failure: Failed to mobilize vault credentials: {e}")
            raise Exception(f"Identity Infrastructure Failure: Vault synchronization compromised: {e}")
            
        subject = "Identity Verification Access Code"
        body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #f0f0f0; border-radius: 10px;">
            <h2 style="color: #ed213a; text-align: center;">AI-PRIORI Authentication</h2>
            <p>Your one-time identity verification code is:</p>
            <div style="background-color: #f8f9fa; padding: 20px; text-align: center; font-size: 32px; font-weight: bold; letter-spacing: 12px; color: #1e293b; border-radius: 8px; margin: 20px 0;">
                {otp}
            </div>
            <p style="color: #64748b; font-size: 14px;">This code will expire in 10 minutes. If you did not request this code, please secure your sector immediately.</p>
            <hr style="border: 0; border-top: 1px solid #f0f0f0; margin: 20px 0;">
            <p style="color: #94a3b8; font-size: 12px; text-align: center;">Vault-Secured Intelligence Portal | AI-PRIORI</p>
        </div>
        """
        return self._send_via_gmail(to_email, subject, body, creds)

    def send_provisioning_email(self, to_email: str, role: str, setup_url: str, creds: Credentials):
        """
        Operator Provisioning Dispatch.
        Dispatches authoritative welcome and initial credentials to newly established Administrative or User sectors.
        """
        subject = f"Mission Provisioning: {role.replace('_', ' ').title()} Access Authorized"
        
        # Brand Alignment Palette
        COLOR_PRIMARY = "#FE1919"
        COLOR_SECONDARY = "#0073B1"
        COLOR_ACCENT = "#F8931F"
        COLOR_DARK = "#111827"
        
        body = f"""
        <div style="font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 20px auto; padding: 40px; border: 1px solid #f1f5f9; border-radius: 24px; color: #1e293b; background-color: #ffffff; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
            <div style="text-align: center; margin-bottom: 40px;">
                <h1 style="color: {COLOR_PRIMARY}; margin: 0; font-size: 32px; font-weight: 900; letter-spacing: -1.5px; text-transform: uppercase;">AI-PRIORI</h1>
                <div style="margin-top: 8px; text-align: center;">
                    <span style="font-size: 10px; font-weight: 800; color: {COLOR_SECONDARY}; letter-spacing: 2px; text-transform: uppercase;">DATA</span>
                    <span style="color: #cbd5e1; margin: 0 8px; font-size: 10px;">-</span>
                    <span style="font-size: 10px; font-weight: 800; color: {COLOR_ACCENT}; letter-spacing: 2px; text-transform: uppercase;">INTELLIGENCE</span>
                    <span style="color: #cbd5e1; margin: 0 8px; font-size: 10px;">-</span>
                    <span style="font-size: 10px; font-weight: 800; color: {COLOR_PRIMARY}; letter-spacing: 2px; text-transform: uppercase;">AUTONOMY</span>
                </div>
            </div>
            
            <div style="border-left: 4px solid {COLOR_PRIMARY}; padding-left: 20px; margin-bottom: 30px;">
                <h2 style="font-size: 20px; font-weight: 800; color: {COLOR_DARK}; margin: 0;">Identity Provisioned</h2>
                <p style="margin-top: 5px; color: #64748b; font-size: 14px;">Sector clearance level: <strong>{role.replace('_', ' ').upper()}</strong></p>
            </div>
            
            <p style="line-height: 1.6; color: #475569; font-size: 15px;">Your operational credentials for the <strong>AI-PRIORI</strong> Intelligence Sector have been initialized. Authorized access is now granted to your registered identity.</p>
            
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 20px; padding: 30px; margin: 35px 0;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td>
                            <span style="font-size: 10px; font-weight: 800; color: #94a3b8; text-transform: uppercase; letter-spacing: 1.5px; display: block; margin-bottom: 5px;">Authorized Identity</span>
                            <span style="font-size: 16px; font-weight: 700; color: {COLOR_DARK};">{to_email}</span>
                        </td>
                    </tr>
                </table>
            </div>

            <p style="line-height: 1.6; color: #475569; font-size: 14px; text-align: center;">To secure your sector and initialize your mission, you must establish your private security credentials.</p>
            
            <div style="text-align: center; margin-top: 35px;">
                <a href="{setup_url}" style="background-color: {COLOR_DARK}; color: #ffffff; padding: 18px 40px; border-radius: 14px; text-decoration: none; font-weight: 800; font-size: 14px; text-transform: uppercase; letter-spacing: 2px; display: inline-block; box-shadow: 0 10px 15px -3px rgba(17, 24, 39, 0.2);">Setup Account & Login</a>
            </div>
            
            <div style="margin-top: 50px; padding-top: 25px; border-top: 1px solid #f1f5f9; text-align: center;">
                <p style="font-size: 11px; color: #94a3b8; line-height: 1.8; margin: 0;">
                    <strong>Security Notice:</strong> This setup link will expire in 24 hours. <br>
                    Unauthorized access to this identity or platform is strictly prohibited and monitored.
                </p>
                <div style="margin-top: 15px; font-size: 10px; color: #cbd5e1; font-weight: 700; letter-spacing: 3px; text-transform: uppercase;">
                    Precision Sector Intelligence
                </div>
            </div>
        </div>
        </div>
        """
        return self._send_via_gmail(to_email, subject, body, creds)

email_service = EmailService()
