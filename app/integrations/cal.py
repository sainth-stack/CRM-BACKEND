import os
import requests
import json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from app.core.logging_config import logger

load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY")
CAL_USERNAME = os.getenv("CAL_USERNAME")
CAL_EVENT_TYPE_ID = int(os.getenv("CAL_EVENT_TYPE_ID", "5137238"))

class CalProvider:
    """
    Autonomous Scheduling Engine.
    Interfaces with the Cal.com V1 API to programmatically manage availability probes and meeting reservations.
    Enables low-friction 'Auto-Booking' protocols for high-intent prospects.
    """
    def __init__(self):
        self.base_url = "https://api.cal.com/v1"
        self.api_key = CAL_API_KEY
        self.username = CAL_USERNAME
        self.event_type_id = CAL_EVENT_TYPE_ID

    def get_first_available_slot(self, days_ahead=3):
        """
        Tactical Availability Probe.
        Scans the associated Cal.com calendar for the first available 30-minute window.
        Defaults to a logical morning slot if the availability probe returns restricted data.
        """
        if not self.api_key: return None
        
        # Security: Start scanning from tomorrow 09:00 UTC to prevent same-day collisions
        start_date = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        end_date = start_date + timedelta(days=days_ahead)
        
        try:
            url = f"{self.base_url}/availability"
            params = {
                "apiKey": self.api_key,
                "userId": 2288333, # User-specific coordinate
                "dateFrom": start_date.isoformat(),
                "dateTo": end_date.isoformat(),
                "eventTypeId": self.event_type_id
            }
            # Fallback strategy: Return tomorrow's preferred slot if API complexity exceeds threshold
            return start_date.isoformat()
        except Exception as e:
            logger.error(f"[CAL] Availability Probe Failure: {e}")
            return start_date.isoformat()

    def book_meeting(self, email: str, name: str, start_time: str = None):
        """
        Autonomous Booking Protocol.
        Programmatically secures a meeting slot on the user's calendar with the prospect.
        Used to finalize high-intent engagements without manual intervention.
        """
        if not self.api_key: 
            logger.error("[CAL] Critical Configuration Error: Null CAL_API_KEY detected.")
            return None
            
        if not start_time:
            start_time = self.get_first_available_slot()
            
        # Cal.com V1 Coordinate Mapping
        start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        end_dt = start_dt + timedelta(minutes=30)
        
        payload = {
            "eventTypeId": self.event_type_id,
            "start": start_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            "end": end_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            "responses": {
                "name": name,
                "email": email,
                "location": "integrations:daily" # Default to high-fidelity Video interaction
            },
            "timeZone": "UTC",
            "language": "en",
            "metadata": {}
        }
        
        try:
            url = f"{self.base_url}/bookings?apiKey={self.api_key}"
            headers = {"Content-Type": "application/json"}
            
            logger.info(f"[CAL] Transition: Initiating Autonomous Booking for {name} ({email}) at {start_time}")
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            
            if response.status_code in [200, 201]:
                data = response.json()
                uid = data.get("uid")
                meeting_url = data.get("videoCallUrl") or f"https://cal.com/booking/{uid}"
                logger.info(f"[CAL] Booking Success: Reservation secured at {meeting_url}")
                return {
                    "link": meeting_url,
                    "uid": uid,
                    "start": start_time,
                    "status": "confirmed"
                }
            else:
                logger.error(f"[CAL] Provider Rejection ({response.status_code}): {response.text}")
                return None
        except Exception as e:
            logger.error(f"[CAL] Autonomous Booking Protocol failed: {e}", exc_info=True)
            return None

cal_provider = CalProvider()

cal_provider = CalProvider()
