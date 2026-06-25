import os
from datetime import datetime, timedelta, timezone
import requests
from app.core.logging_config import logger
from app.core.config import settings

CAL_API_KEY = settings.CAL_API_KEY
CAL_EVENT_TYPE_ID = settings.CAL_EVENT_TYPE_ID
CAL_TIMEZONE = settings.CAL_TIMEZONE


from app.core.retry_utils import with_retries

class CalProvider:
    """
    Scheduling integration with Cal.com API v2.
    Supports multi-user OAuth token resolution and dynamic calendar booking.
    """

    def __init__(self):
        self.base_url = "https://api.cal.com/v2"

    def get_event_types(self, db, user) -> list[dict]:
        """
        Fetches all event types for the authenticated user from Cal.com.
        Returns a list of dicts with id, title, slug, and duration.
        """
        token = self.get_valid_access_token(db, user)
        if not token:
            return []
        try:
            response = requests.get(
                f"{self.base_url}/event-types",
                headers={
                    "Authorization": f"Bearer {token}",
                    "cal-api-version": "2024-06-14",
                },
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            # API v2 can return { status: "success", data: [...] } or { status: "success", data: { eventTypeGroups: [...] } }
            data = body.get("data", [])
            results = []
            
            if isinstance(data, list):
                for et in data:
                    results.append({
                        "id": et.get("id"),
                        "title": et.get("title"),
                        "slug": et.get("slug"),
                        "duration": et.get("length") or et.get("duration"),
                    })
            elif isinstance(data, dict):
                # 1. Fallback for nested eventTypeGroups structure
                event_type_groups = data.get("eventTypeGroups", [])
                for group in event_type_groups:
                    for et in group.get("eventTypes", []):
                        results.append({
                            "id": et.get("id"),
                            "title": et.get("title"),
                            "slug": et.get("slug"),
                            "duration": et.get("length") or et.get("duration"),
                        })
                # 2. Check for direct eventTypes list in the dictionary
                for et in data.get("eventTypes", []):
                    results.append({
                        "id": et.get("id"),
                        "title": et.get("title"),
                        "slug": et.get("slug"),
                        "duration": et.get("length") or et.get("duration"),
                    })
                    
            logger.info(f"[CAL] Successfully fetched and parsed {len(results)} event types for {user.email}")
            return results
        except Exception as e:
            logger.error(f"[CAL] Failed to fetch event types: {e}", exc_info=True)
            return []

    def get_valid_access_token(self, db, user) -> str | None:
        """
        Decrypted Cal.com access token for the user, refreshing it synchronously if expired.
        Requires the user to have a connected Cal.com account.
        """
        if not user or not user.cal_refresh_token:
            logger.warning(f"[CAL] User {user.email if user else 'anonymous'} has no connected Cal.com account.")
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
                    
                db.flush()
                logger.info(f"[CAL] Access token successfully refreshed for user {user.email}")
            except Exception as e:
                error_str = str(e).lower()
                # 400 Bad Request = the refresh token itself is invalid/revoked.
                # Auto-clear stale tokens so the frontend can detect this and prompt re-auth.
                is_unrecoverable = "400" in error_str or "bad request" in error_str or "invalid" in error_str
                if is_unrecoverable:
                    logger.warning(
                        f"[CAL] Refresh token for {user.email} is invalid or revoked. "
                        "Clearing stale credentials — user must re-authorize Cal.com."
                    )
                    user.cal_access_token = None
                    user.cal_refresh_token = None
                    user.cal_token_expires_at = None
                    try:
                        db.flush()
                    except Exception:
                        pass
                else:
                    logger.error(f"[CAL] Failed to refresh access token for user {user.email}: {e}")
                return None

        try:
            return decrypt_token(user.cal_access_token)
        except Exception as e:
            logger.error(f"[CAL] Failed to decrypt access token for user {user.email}: {e}")
            return None

    @with_retries(max_attempts=3, base_delay=2.0)
    def get_first_available_slot(self, db, user, days_ahead: int = 3):
        token = self.get_valid_access_token(db, user)
        if not token:
            logger.warning("[CAL] Cannot fetch availability slots: Cal.com account not connected or token invalid. Please connect your Cal.com account in Settings.")
            return None

        event_type_id = (user.cal_event_type_id if user else None) or CAL_EVENT_TYPE_ID
        if not event_type_id:
            logger.warning(f"[CAL] User has no configured event type and no global CAL_EVENT_TYPE_ID fallback configured.")
            return None

        timezone_str = (user.cal_timezone if user else None) or CAL_TIMEZONE or "UTC"

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
            raise exc

    @with_retries(max_attempts=3, base_delay=2.0)
    def get_upcoming_available_slots(self, db, user, days_ahead: int = None, limit: int = None) -> list[str]:
        days_ahead = days_ahead if days_ahead is not None else settings.CAL_SLOTS_LOOKAHEAD_DAYS
        limit      = limit      if limit      is not None else settings.CAL_SLOTS_LIMIT
        token = self.get_valid_access_token(db, user)
        if not token:
            logger.warning("[CAL] Cannot fetch availability slots: Cal.com account not connected or token invalid. Please connect your Cal.com account in Settings.")
            return []

        event_type_id = (user.cal_event_type_id if user else None) or CAL_EVENT_TYPE_ID
        if not event_type_id:
            logger.warning(f"[CAL] User has no configured event type and no global CAL_EVENT_TYPE_ID fallback configured.")
            return []

        timezone_str = (user.cal_timezone if user else None) or CAL_TIMEZONE or "UTC"

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
            return self._extract_slots(response.json(), limit=limit)
        except Exception as exc:
            logger.error(f"[CAL] Availability slots fetch failure: {exc}", exc_info=True)
            return []

    @with_retries(max_attempts=3, base_delay=2.0)
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
            logger.warning(f"[CAL] Cannot book meeting for {email}: Cal.com account is not connected or token could not be refreshed. Please connect Cal.com in Settings.")
            return None

        event_type_id = (user.cal_event_type_id if user else None) or CAL_EVENT_TYPE_ID
        if not event_type_id:
            logger.warning(f"[CAL] Cannot book meeting for {email}: User has no configured cal_event_type_id and no global fallback configured.")
            return None

        timezone_str = (user.cal_timezone if user else None) or CAL_TIMEZONE or "UTC"

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
            raise exc

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

    def _extract_slots(self, payload: dict, limit: int = 5) -> list[str]:
        slots = []
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
                        slots.append(slot)
                        if len(slots) >= limit:
                            return slots
        return slots

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
