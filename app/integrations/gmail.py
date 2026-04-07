import os
import base64
import json
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

class GmailProvider:
    def __init__(self, creds: Credentials = None):
        """
        Initializes the GMail provider for a specific sector (user).
        The creds are usually generated dynamically via the TokenService.
        """
        self.creds = creds
        
    def get_latest_replies(self):
        """Scans the inbox for recent prospect replies."""
        if not self.creds:
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
            print(f"[GMAIL-IN] Scanner Failure: {e}")
            return []

    def mark_as_read(self, message_id):
        """Removes the UNREAD label from a message."""
        if not self.creds: return
        try:
            service = build('gmail', 'v1', credentials=self.creds)
            service.users().messages().batchModify(userId='me', body={'ids': [message_id], 'removeLabelIds': ['UNREAD']}).execute()
        except: pass

# Export a simple factory or class for use within the worker/service layers
# The credentials will be injected by the TokenService in those contexts.
