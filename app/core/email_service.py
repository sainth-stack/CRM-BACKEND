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
        # Cache Gmail service objects to avoid re-parsing the API discovery doc.
        self._service_cache = {}
    
    def send_email(self, to_email: str, subject: str, body: str, creds: Credentials, thread_id: str = None) -> dict:
        """Send an email through the user's connected mailbox."""
        if not creds:
            logger.error(f"Email failed: No credentials for {to_email}")
            raise Exception("No mailbox credentials provided.")

        try:
            return self._send_via_gmail(to_email, subject, body, creds, thread_id)
        except Exception as e:
            logger.error(f"[EMAIL] Failed to send to {to_email}: {e}", exc_info=True)
            raise e

    def _get_service(self, creds: Credentials) -> Any:
        """Build (and cache) a Gmail API client for the given credentials."""
        # Key the cache by refresh token (or temporary access token).
        cache_key = hash(creds.refresh_token or creds.token)

        if cache_key in self._service_cache:
            return self._service_cache[cache_key]

        # Build a new Gmail client.
        service = build('gmail', 'v1', credentials=creds)
        self._service_cache[cache_key] = service
        return service

    @with_retries(max_attempts=3, base_delay=3.0, exceptions=(HttpError, httplib2.ServerNotFoundError, ConnectionError))
    def _send_via_gmail(self, to_email: str, subject: str, body: str, creds: Credentials, thread_id: str = None) -> dict:
        """Send via the Gmail API (supports threading) with a 10s timeout."""
        service = self._get_service(creds)

        message = MIMEMultipart()
        message['To'] = to_email
        message['Subject'] = subject

        # Thread the reply when a thread id is supplied.
        if thread_id:
            message['In-Reply-To'] = thread_id
            message['References'] = thread_id

        is_html = any(tag in body.lower() for tag in ["<html", "<div", "<p", "<table", "<body"])
        
        if not is_html:
            # Convert plain text to HTML so the client doesn't hard-wrap at 70 chars.
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
        """Send an OTP verification email using the system mailbox credentials (GMAIL_TOKEN_JSON)."""
        import json
        from app.core.config import settings
        token_json = settings.GMAIL_TOKEN_JSON
        if not token_json:
            logger.error("GMAIL_TOKEN_JSON is not set in the environment.")
            raise Exception("GMAIL_TOKEN_JSON is not configured.")

        try:
            data = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(data)
        except Exception as e:
            logger.error(f"Failed to load system mailbox credentials: {e}")
            raise Exception(f"Failed to load system mailbox credentials: {e}")
            
        subject = "Your FocalReach verification code"
        # FocalReach brand: deep dark + cyan accent ("Reach" in cyan).
        body = f"""
        <div style="font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 32px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #ffffff;">
            <div style="text-align: center; margin-bottom: 28px;">
                <h2 style="margin: 0; font-size: 22px; font-weight: 900; letter-spacing: -0.5px; color: #0A0E1A;">Focal<span style="color: #00f0ff;">Reach</span></h2>
                <div style="margin-top: 4px; font-size: 10px; font-weight: 800; color: #94A3B8; letter-spacing: 3px; text-transform: uppercase;">AI Outreach</div>
            </div>
            <p style="color: #475569; font-size: 15px; line-height: 1.6; margin: 0 0 16px;">Your one-time verification code is:</p>
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; padding: 22px; text-align: center; font-size: 32px; font-weight: bold; letter-spacing: 12px; color: #0A0E1A; border-radius: 12px; margin: 20px 0;">
                {otp}
            </div>
            <p style="color: #64748b; font-size: 13px; line-height: 1.6;">This code expires in 10 minutes. If you didn't request it, you can safely ignore this email.</p>
            <hr style="border: 0; border-top: 1px solid #f1f5f9; margin: 24px 0 16px;">
            <p style="color: #94a3b8; font-size: 11px; text-align: center; letter-spacing: 2px; text-transform: uppercase; margin: 0;">FocalReach</p>
        </div>
        """
        return self._send_via_gmail(to_email, subject, body, creds)

    def send_provisioning_email(self, to_email: str, role: str, setup_url: str, creds: Credentials):
        """Send the welcome / account-setup email to a newly created admin or user.

        Styled to match the FocalReach in-app brand: deep brand-dark text on a clean
        light card, with the signature "Reach" cyan accent (#00f0ff) on the wordmark
        and the CTA glow. Inline styles + table-friendly markup so it renders well
        across Gmail / Outlook / Apple Mail without dark-mode quirks.
        """
        role_label = role.replace("_", " ").title()
        subject = f"Your FocalReach {role_label} account is ready"

        # FocalReach brand palette
        BRAND_DARK = "#0A0E1A"      # primary dark — wordmark, headings, CTA bg
        BRAND_CYAN = "#00f0ff"      # signature accent (used on "Reach", CTA glow, divider)
        BODY = "#475569"
        MUTED = "#94A3B8"
        CARD_BORDER = "#e2e8f0"
        PAGE_BG = "#F8FAFC"

        body = f"""
        <div style="background-color: {PAGE_BG}; padding: 32px 16px; font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
          <div style="max-width: 600px; margin: 0 auto; padding: 40px; border: 1px solid {CARD_BORDER}; border-radius: 20px; color: #1e293b; background-color: #ffffff; box-shadow: 0 4px 6px -1px rgba(10, 14, 26, 0.06);">

            <!-- Brand -->
            <div style="text-align: center; margin-bottom: 36px;">
              <h1 style="margin: 0; font-size: 30px; font-weight: 900; letter-spacing: -1px; color: {BRAND_DARK};">
                Focal<span style="color: {BRAND_CYAN};">Reach</span>
              </h1>
              <div style="margin-top: 6px; font-size: 10px; font-weight: 800; color: {MUTED}; letter-spacing: 3px; text-transform: uppercase;">
                AI Outreach
              </div>
            </div>

            <!-- Headline -->
            <div style="border-left: 4px solid {BRAND_CYAN}; padding-left: 20px; margin-bottom: 28px;">
              <h2 style="font-size: 20px; font-weight: 800; color: {BRAND_DARK}; margin: 0;">Account created</h2>
              <p style="margin: 6px 0 0; color: #64748b; font-size: 14px;">Role: <strong style="color: {BRAND_DARK};">{role_label.upper()}</strong></p>
            </div>

            <p style="line-height: 1.65; color: {BODY}; font-size: 15px; margin: 0 0 24px;">
              A <strong style="color: {BRAND_DARK};">FocalReach</strong> account has been created for you. Set your password using the link below to get started.
            </p>

            <!-- Account email panel -->
            <div style="background-color: #f8fafc; border: 1px solid {CARD_BORDER}; border-radius: 14px; padding: 22px 26px; margin: 28px 0;">
              <table style="width: 100%; border-collapse: collapse;">
                <tr>
                  <td>
                    <span style="font-size: 10px; font-weight: 800; color: {MUTED}; text-transform: uppercase; letter-spacing: 1.5px; display: block; margin-bottom: 6px;">Account email</span>
                    <span style="font-size: 16px; font-weight: 700; color: {BRAND_DARK};">{to_email}</span>
                  </td>
                </tr>
              </table>
            </div>

            <p style="line-height: 1.6; color: {BODY}; font-size: 14px; text-align: center; margin: 0 0 8px;">
              To finish setting up your account, create your password.
            </p>

            <!-- CTA: dark button, cyan glow -->
            <div style="text-align: center; margin: 28px 0 8px;">
              <a href="{setup_url}" style="background-color: {BRAND_DARK}; color: #ffffff; padding: 16px 40px; border-radius: 12px; text-decoration: none; font-weight: 800; font-size: 14px; text-transform: uppercase; letter-spacing: 2px; display: inline-block; box-shadow: 0 10px 24px -6px rgba(0, 240, 255, 0.45);">
                Set Up &amp; Sign In
              </a>
            </div>

            <!-- Footer -->
            <div style="margin-top: 44px; padding-top: 22px; border-top: 1px solid #f1f5f9; text-align: center;">
              <p style="font-size: 11px; color: {MUTED}; line-height: 1.8; margin: 0;">
                <strong style="color: {BRAND_DARK};">Security notice:</strong> This setup link expires in 24 hours.<br>
                If you didn't expect this email, you can safely ignore it.
              </p>
              <div style="margin-top: 14px; font-size: 10px; color: #cbd5e1; font-weight: 700; letter-spacing: 3px; text-transform: uppercase;">
                FocalReach
              </div>
            </div>
          </div>
        </div>
        """
        return self._send_via_gmail(to_email, subject, body, creds)

email_service = EmailService()
