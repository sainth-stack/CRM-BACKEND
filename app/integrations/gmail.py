import os
import base64
import json
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from app.core.logging_config import logger

class GmailProvider:
    """
    Sovereign Gmail Integration Layer.
    Provides high-fidelity abstractions for scanning, reading, and managing corporate email communications
    for a specific user sector using established OAuth2 credentials.
    """
    def __init__(self, creds: Credentials = None):
        """
        Initializes the Gmail provider for a specific sector (user).
        Credentials are authenticated and provisioned dynamically via the TokenService.
        """
        self.creds = creds
        
    def get_latest_replies(self):
        """
        Inbox Sentinel Logic.
        Scans the INBOX for recent messages, extracts structured payload data, and decodes content for AI intent classification.
        """
        if not self.creds:
            logger.warning("[GMAIL-IN] Aborting inbox scan: Null credentials provided.")
            return []

        try:
            service = build('gmail', 'v1', credentials=self.creds)
            
            # 1. Search for recent messages in INBOX
            query = "label:INBOX" 
            results = service.users().messages().list(userId='me', q=query, maxResults=15).execute()
            messages = results.get('messages', [])
            
            if not messages:
                return []
  
            replies = []
            for msg_meta in messages:
                msg = service.users().messages().get(userId='me', id=msg_meta['id'], format='full').execute()
                
                payload = msg.get('payload', {})
                headers = payload.get('headers', [])
                header_dict = {h['name']: h['value'] for h in headers}
                
                body = ""
                def get_part_content(payload_parts):
                    text = ""
                    for part in payload_parts:
                        if part['mimeType'] == 'text/plain':
                            data = part['body'].get('data', '')
                            if data:
                                text += base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                        elif 'parts' in part:
                            text += get_part_content(part['parts'])
                    return text

                if 'parts' in payload:
                    body = get_part_content(payload['parts'])
                else:
                    body_data = payload.get('body', {}).get('data', '')
                    if body_data:
                        body = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='ignore')

                replies.append({
                    "message_id": msg.get('id'),
                    "thread_id": msg.get('threadId'),
                    "in_reply_to": header_dict.get('In-Reply-To', ''),
                    "references": header_dict.get('References', ''),
                    "from": header_dict.get('From', ''),
                    "subject": header_dict.get('Subject', ''),
                    "body": body,
                    "raw_msg_id": header_dict.get('Message-ID', '')
                })
  
            return replies

        except Exception as e:
            logger.error(f"[GMAIL-IN] Scanner Critical Failure: {e}", exc_info=True)
            return []

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
