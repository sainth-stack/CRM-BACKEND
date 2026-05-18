import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from app.core.logging_config import logger

load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY")
CAL_EVENT_TYPE_ID = int(os.getenv("CAL_EVENT_TYPE_ID", "5137238"))
CAL_TIMEZONE = os.getenv("CAL_TIMEZONE", "UTC")


class CalProvider:
    """
    Scheduling integration with Cal.com API v2.
    Supports multi-user OAuth token resolution and dynamic calendar booking.
    """

    def __init__(self):
        self.base_url = "https://api.cal.com/v2"

    def get_valid_access_token(self, db, user) -> str | None:
        """
        Retrieves a valid, decrypted Cal.com access token for the user,
        automatically refreshing it synchronously if it has expired.
        """
        if not user or not user.cal_refresh_token:
            logger.warning(f"[CAL] User has no connected Cal.com account.")
            return None

        from app.core.security import decrypt_token, encrypt_token
        from app.core.auth import CalAuthService

        # Check if token is expired or expires in the next 120 seconds
        now = datetime.now()
        is_expired = (
            user.cal_token_expires_at is None or 
            user.cal_token_expires_at <= now + timedelta(seconds=120)
        )

        if is_expired:
            logger.info(f"[CAL] Access token for user {user.email} is expired. Refreshing...")
            try:
                decrypted_refresh = decrypt_token(user.cal_refresh_token)
                tokens = CalAuthService.refresh_access_token(decrypted_refresh)
                
                user.cal_access_token = encrypt_token(tokens["access_token"])
                user.cal_refresh_token = encrypt_token(tokens["refresh_token"])
                
                if tokens.get("expires_at"):
                    import dateutil.parser
                    user.cal_token_expires_at = dateutil.parser.parse(tokens["expires_at"]).replace(tzinfo=None)
                else:
                    user.cal_token_expires_at = datetime.now() + timedelta(seconds=1800)
                    
                db.commit()
                logger.info(f"[CAL] Access token successfully refreshed for user {user.email}")
            except Exception as e:
                logger.error(f"[CAL] Failed to refresh access token for user {user.email}: {e}")
                return None

        try:
            return decrypt_token(user.cal_access_token)
        except Exception as e:
            logger.error(f"[CAL] Failed to decrypt access token for user {user.email}: {e}")
            return None

    def get_first_available_slot(self, db, user, days_ahead: int = 3):
        token = self.get_valid_access_token(db, user)
        if not token:
            logger.warning("[CAL] Cannot fetch availability slots: missing or invalid user token.")
            return None

        event_type_id = user.cal_event_type_id
        if not event_type_id:
            logger.warning(f"[CAL] User {user.email} has no configured cal_event_type_id.")
            return None

        timezone_str = user.cal_timezone or "UTC"

        start_time = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).replace(hour=9, minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(days=days_ahead)

        headers = {
            "Authorization": f"Bearer {token}",
            "cal-api-version": "2024-09-04",
            "Content-Type": "application/json"
        }

        try:
            response = requests.get(
                f"{self.base_url}/slots",
                headers=headers,
                params={
                    "eventTypeId": event_type_id,
                    "start": start_time.strftime("%Y-%m-%d"),
                    "end": end_time.strftime("%Y-%m-%d"),
                    "timeZone": timezone_str,
                },
                timeout=10,
            )
            response.raise_for_status()
            return self._extract_first_slot(response.json())
        except Exception as exc:
            logger.error(f"[CAL] Availability probe failure: {exc}", exc_info=True)
            return None

    def book_meeting(
        self,
        db,
        user,
        email: str,
        name: str,
        start_time: str | None = None,
        booking_timezone: str | None = None,
    ):
        token = self.get_valid_access_token(db, user)
        if not token:
            logger.error("[CAL] Cannot book meeting: missing or invalid user token.")
            return None

        event_type_id = user.cal_event_type_id
        if not event_type_id:
            logger.warning(f"[CAL] User {user.email} has no configured cal_event_type_id.")
            return None

        timezone_str = user.cal_timezone or "UTC"

        slot_start = start_time or self.get_first_available_slot(db, user)
        if not slot_start:
            logger.warning("[CAL] No valid slot available for booking.")
            return None

        target_timezone = booking_timezone or timezone_str
        try:
            start_dt = self._coerce_datetime(slot_start, target_timezone)
        except ValueError as exc:
            logger.error(f"[CAL] Invalid booking start time '{slot_start}': {exc}")
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "cal-api-version": "2024-08-13",
            "Content-Type": "application/json"
        }

        payload = {
            "eventTypeId": event_type_id,
            "start": start_dt.isoformat(timespec="milliseconds"),
            "attendee": {
                "name": name,
                "email": email,
                "timeZone": target_timezone,
                "language": "en"
            },
            "location": {
                "type": "integration",
                "integration": "cal-video"
            },
            "metadata": {},
        }

        try:
            response = requests.post(
                f"{self.base_url}/bookings",
                headers=headers,
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            uid = data.get("uid")
            meeting_url = data.get("meetingUrl") or data.get("videoCallUrl") or data.get("bookingUrl") or data.get("location") or (
                f"https://cal.com/booking/{uid}" if uid else None
            )
            logger.info(f"[CAL] Booking success for {email} at {payload['start']}")
            return {
                "link": meeting_url,
                "uid": uid,
                "start": payload["start"],
                "status": data.get("status", body.get("status", "confirmed")) if isinstance(body, dict) else "confirmed",
            }
        except Exception as exc:
            logger.error(f"[CAL] Booking failed: {exc}", exc_info=True)
            return None

    @staticmethod
    def _coerce_datetime(value: str, target_timezone: str = "UTC") -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        tz = timezone.utc if target_timezone == "UTC" else datetime.now().astimezone().tzinfo
        if target_timezone != "UTC":
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(target_timezone)
            except Exception:
                tz = timezone.utc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)

    def _extract_first_slot(self, payload: dict) -> str | None:
        slot_maps = []

        if isinstance(payload.get("slots"), dict):
            slot_maps.append(payload["slots"])

        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("slots"), dict):
                slot_maps.append(data["slots"])
            else:
                # API v2 shape: data is the dict with date keys
                slot_maps.append(data)

        for slot_map in slot_maps:
            for day in sorted(slot_map.keys()):
                # Filter out metadata keys like 'status' or 'success' if present at top level in API v2
                if day in ("status", "success"):
                    continue
                entries = slot_map.get(day) or []
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    slot = self._extract_slot_value(entry)
                    if slot:
                        return slot
        return None

    @staticmethod
    def _extract_slot_value(entry) -> str | None:
        if isinstance(entry, str):
            return entry
        if isinstance(entry, dict):
            for key in ("time", "start", "slotStart"):
                value = entry.get(key)
                if value:
                    return value
        return None


cal_provider = CalProvider()
