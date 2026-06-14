import base64
import json
from datetime import UTC, datetime
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.exceptions import RefreshError
from app.core.circuit_breaker import (
    CircuitBreakerConfig,
    circuit_is_open,
    record_circuit_failure,
    record_circuit_success,
)
from app.core.logging_config import logger
from app.core.retry_utils import with_retries

GMAIL_SCAN_CIRCUIT = CircuitBreakerConfig(
    name="gmail:scan",
    failure_threshold=3,
    recovery_timeout_seconds=180,
)


class GmailProvider:
    """Gmail integration: scan, read, and manage a user's email via OAuth2 credentials."""
    def __init__(self, creds: Credentials = None):
        """Initialize the provider for a user. Credentials are supplied by the TokenService."""
        self.creds = creds
        
    def scan_latest_replies(self, unread_only: bool = True) -> dict:
        """Scan for unread messages, parsing MIME recursively to extract their content."""
        if not self.creds:
            return {
                "status": "NO_CREDENTIALS",
                "replies": [],
                "error": "Mailbox credentials were not available for Gmail polling.",
            }

        is_open, state = circuit_is_open(GMAIL_SCAN_CIRCUIT)
        if is_open:
            return {
                "status": "CIRCUIT_OPEN",
                "replies": [],
                "error": state.get("last_failure") or "Gmail polling temporarily paused after repeated provider failures.",
            }

        try:
            service = build('gmail', 'v1', credentials=self.creds)
            
            # 1. High-Performance Query: recent unread inbox messages only
            query = "label:INBOX newer_than:30d"
            if unread_only:
                query += " label:UNREAD"
                
            messages = []
            next_page = None
            # Limit to 5 pages (75 messages) per poll
            for _ in range(5):
                results = service.users().messages().list(
                    userId='me', q=query, maxResults=15, pageToken=next_page
                ).execute()
                messages.extend(results.get('messages', []))
                next_page = results.get('nextPageToken')
                if not next_page: break
            
            if not messages:
                record_circuit_success(GMAIL_SCAN_CIRCUIT)
                return {"status": "HEALTHY", "replies": [], "error": None}
  
            replies = []
            for msg_meta in messages:
                msg = service.users().messages().get(userId='me', id=msg_meta['id'], format='full').execute()
                
                payload = msg.get('payload', {})
                headers = payload.get('headers', [])
                header_dict = {h['name']: h['value'] for h in headers}
                
                # RECURSIVE CONTENT EXTRACTION (Supports HTML fallbacks and nested structures)
                def get_part_content(part):
                    mime_type = part.get('mimeType')
                    body_data = part.get('body', {}).get('data', '')
                    content = ""
                    
                    if body_data:
                        try:
                            content = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='ignore')
                        except: pass

                    if mime_type == 'text/plain':
                        return content
                    elif mime_type == 'text/html' and not content.strip() == "":
                        # Simple HTML cleanup for LLM consumption
                        return content
                    
                    # Recursive walk for multipart alternatives
                    sub_parts = part.get('parts', [])
                    combined = ""
                    for sp in sub_parts:
                        combined += get_part_content(sp)
                    return combined

                body = get_part_content(payload)

                replies.append({
                    "message_id": msg.get('id'),
                    "thread_id": msg.get('threadId'),
                    "in_reply_to": header_dict.get('In-Reply-To', ''),
                    "references": header_dict.get('References', ''),
                    "from": header_dict.get('From', ''),
                    "subject": header_dict.get('Subject', ''),
                    "body": body,
                    "raw_msg_id": header_dict.get('Message-ID', ''),
                    "received_at": _parse_internal_date(msg.get('internalDate')),
                })
  
            record_circuit_success(GMAIL_SCAN_CIRCUIT)
            return {"status": "HEALTHY", "replies": replies, "error": None}

        except Exception as e:
            health_status, error_message = _classify_gmail_scan_error(e)
            if health_status in {"QUOTA_ERROR", "TRANSIENT_ERROR"}:
                record_circuit_failure(GMAIL_SCAN_CIRCUIT, error_message)
            logger.error(f"[GMAIL-IN] Scanner Critical Failure ({health_status}): {error_message}", exc_info=True)
            return {
                "status": health_status,
                "replies": [],
                "error": error_message,
            }

    def get_latest_replies(self, unread_only: bool = True):
        return self.scan_latest_replies(unread_only=unread_only).get("replies", [])

    @with_retries(max_attempts=3, base_delay=2.0)
    def mark_as_read(self, message_id):
        """
        Communication Protocol: Inbox Housekeeping.
        Removes the UNREAD label from a specific message to synchronize state across clients.
        """
        if not self.creds: return
        try:
            service = build('gmail', 'v1', credentials=self.creds)
            service.users().messages().batchModify(userId='me', body={'ids': [message_id], 'removeLabelIds': ['UNREAD']}).execute()
        except Exception as e:
            logger.debug(f"[GMAIL-OUT] State sync error: {e}")

# Export a simple factory or class for use within the worker/service layers
# The credentials will be injected by the TokenService in those contexts.


def _parse_internal_date(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def _classify_gmail_scan_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, RefreshError):
        return "AUTH_ERROR", f"Token has been expired or revoked: {str(error)}"

    if isinstance(error, HttpError):
        status_code = getattr(getattr(error, "resp", None), "status", None)
        details = ""
        try:
            payload = json.loads(error.content.decode("utf-8"))
            details = payload.get("error", {}).get("message", "")
        except Exception:
            details = str(error)

        if status_code == 401:
            return "AUTH_ERROR", details or "Gmail access token expired or unauthorized."
        if status_code == 403:
            lowered = (details or "").lower()
            if "quota" in lowered or "rate" in lowered:
                return "QUOTA_ERROR", details or "Gmail API quota exceeded."
            return "PERMISSION_ERROR", details or "Gmail permissions were rejected."
        if status_code == 429:
            return "QUOTA_ERROR", details or "Gmail API quota exceeded."
        if status_code and 500 <= status_code < 600:
            return "TRANSIENT_ERROR", details or "Transient Gmail server failure."
        return "TRANSIENT_ERROR", details or str(error)

    return "TRANSIENT_ERROR", str(error)
