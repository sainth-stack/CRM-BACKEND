import os
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

class EmailService:
    """
    Production-grade stateless email service for multi-tenant outreach.
    Requires external injection of GMail Credentials.
    """
    
    def send_email(self, to_email: str, subject: str, body: str, creds: Credentials, thread_id: str = None) -> dict:
        """
        Routes outreach distribution through the provided user sector credentials.
        """
        if not creds:
            raise Exception("Security Error: Outreach capability blocked. No mailbox synchronization identified for this user.")
        
        try:
            return self._send_via_gmail(to_email, subject, body, creds, thread_id)
        except Exception as e:
            print(f"[EMAIL] Outreach Dispatch Failure: {e}")
            raise e

    def _send_via_gmail(self, to_email: str, subject: str, body: str, creds: Credentials, thread_id: str = None) -> dict:
        """Official Google API Bridge for high-fidelity outreach and threading."""
        service = build('gmail', 'v1', credentials=creds)
        
        message = MIMEMultipart()
        message['To'] = to_email
        message['Subject'] = subject
        
        # High-Fidelity Threading
        if thread_id:
            message['In-Reply-To'] = thread_id
            message['References'] = thread_id

        is_html = any(tag in body.lower() for tag in ["<html", "<div", "<p", "<table", "<body"])
        
        if is_html:
            # Wrap in standard HTML perimeter if not present
            if "<html>" not in body.lower():
                body = f"<html><body style='margin:0;padding:0;'>{body}</body></html>"
            msg_type = MIMEText(body, 'html')
        else:
            msg_type = MIMEText(body, 'plain')
            
        message.attach(msg_type)
        
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        data = {'raw': raw_message}
        if thread_id:
            data['threadId'] = thread_id
            
        try:
            sent_msg = service.users().messages().send(userId='me', body=data).execute()
            msg_id = sent_msg.get('id')
            new_thread_id = sent_msg.get('threadId')
            print(f"[GMAIL] Mission Success: Dispatched to {to_email} (Msg ID: {msg_id}, Thread ID: {new_thread_id})")
            return {"id": msg_id, "thread_id": new_thread_id}
        except Exception as e:
            print(f"[GMAIL] API Error during outreach to {to_email}: {e}")
            raise e

    def send_otp_email(self, to_email: str, otp: str):
        """Sends a system-critical identity verification code (OTP) via the core vault credentials."""
        token_path = os.path.join(os.getcwd(), 'token.json')
        if not os.path.exists(token_path):
            # Fallback for Render if stored in env var as JSON
            import json
            token_json = os.getenv("GMAIL_TOKEN_JSON")
            if token_json:
                data = json.loads(token_json)
                creds = Credentials.from_authorized_user_info(data)
            else:
                raise Exception("Identity Infrastructure Failure: System vault (token.json) not identified.")
        else:
            creds = Credentials.from_authorized_user_file(token_path)
            
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

    def send_provisioning_email(self, to_email: str, role: str, password: str, creds: Credentials):
        """Dispatches an authoritative welcome and credential handover to new operators."""
        subject = f"Mission Provisioning: {role.replace('_', ' ').title()} Access Authorized"
        
        # Determine Login URL (ideally from env, but defaulting to common local/prod paths)
        login_url = os.getenv("FRONTEND_URL", "http://localhost:5173") + "/login"
        
        # Brand Alignment Palette
        COLOR_PRIMARY = "#FE1919"    # Red
        COLOR_SECONDARY = "#0073B1"  # Data Blue
        COLOR_ACCENT = "#F8931F"     # Intelligence Orange
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
            
            <p style="line-height: 1.6; color: #475569; font-size: 15px;">Your operational credentials for the <strong>AI-PRIORI</strong> Intelligence Sector have been initialized. Authorized access is now granted to your register identity.</p>
            
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 20px; padding: 30px; margin: 35px 0;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding-bottom: 20px;">
                            <span style="font-size: 10px; font-weight: 800; color: #94a3b8; text-transform: uppercase; letter-spacing: 1.5px; display: block; margin-bottom: 5px;">Authorized Identity</span>
                            <span style="font-size: 16px; font-weight: 700; color: {COLOR_DARK};">{to_email}</span>
                        </td>
                    </tr>
                    <tr>
                        <td>
                            <span style="font-size: 10px; font-weight: 800; color: #94a3b8; text-transform: uppercase; letter-spacing: 1.5px; display: block; margin-bottom: 5px;">Security Handshake</span>
                            <span style="font-size: 18px; font-weight: 800; color: {COLOR_PRIMARY}; font-family: 'Courier New', Courier, monospace; letter-spacing: 1px;">{password}</span>
                        </td>
                    </tr>
                </table>
            </div>
            
            <div style="text-align: center; margin-top: 45px;">
                <a href="{login_url}" style="background-color: {COLOR_DARK}; color: #ffffff; padding: 18px 40px; border-radius: 14px; text-decoration: none; font-weight: 800; font-size: 14px; text-transform: uppercase; letter-spacing: 2px; display: inline-block; box-shadow: 0 10px 15px -3px rgba(17, 24, 39, 0.2);">Initialize Session</a>
            </div>
            
            <div style="margin-top: 50px; padding-top: 25px; border-top: 1px solid #f1f5f9; text-align: center;">
                <p style="font-size: 11px; color: #94a3b8; line-height: 1.8; margin: 0;">
                    <strong>Mission-Critical Notice:</strong> This is an encrypted dispatch from the AI-PRIORI Governance Sector. <br>
                    Unauthorized access to this identity or platform is strictly prohibited and monitored.
                </p>
                <div style="margin-top: 15px; font-size: 10px; color: #cbd5e1; font-weight: 700; letter-spacing: 3px; text-transform: uppercase;">
                    Precision Sector Intelligence
                </div>
            </div>
        </div>
        """
        return self._send_via_gmail(to_email, subject, body, creds)

email_service = EmailService()
