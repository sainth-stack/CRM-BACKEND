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
    Scheduling integration with Cal.com.
    Keeps the existing workflow intact while making availability resolution deterministic.
    """

    def __init__(self):
        self.base_url = "https://api.cal.com/v1"
        self.api_key = CAL_API_KEY
        self.event_type_id = CAL_EVENT_TYPE_ID
        self.timezone = CAL_TIMEZONE

    def get_first_available_slot(self, days_ahead: int = 3):
        if not self.api_key:
            return None

        start_time = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).replace(hour=9, minute=0, second=0, microsecond=0)
        end_time = start_time + timedelta(days=days_ahead)

        try:
            response = requests.get(
                f"{self.base_url}/slots",
                params={
                    "apiKey": self.api_key,
                    "eventTypeId": self.event_type_id,
                    "startTime": start_time.isoformat(),
                    "endTime": end_time.isoformat(),
                    "timeZone": self.timezone,
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
        email: str,
        name: str,
        start_time: str | None = None,
        booking_timezone: str | None = None,
    ):
        if not self.api_key:
            logger.error("[CAL] Missing CAL_API_KEY.")
            return None

        slot_start = start_time or self.get_first_available_slot()
        if not slot_start:
            logger.warning("[CAL] No valid slot available for booking.")
            return None

        target_timezone = booking_timezone or self.timezone
        try:
            start_dt = self._coerce_datetime(slot_start, target_timezone)
            end_dt = start_dt + timedelta(minutes=30)
        except ValueError as exc:
            logger.error(f"[CAL] Invalid booking start time '{slot_start}': {exc}")
            return None

        payload = {
            "eventTypeId": self.event_type_id,
            "start": start_dt.isoformat(timespec="milliseconds"),
            "end": end_dt.isoformat(timespec="milliseconds"),
            "responses": {
                "name": name,
                "email": email,
                "location": "integrations:daily",
            },
            "timeZone": target_timezone,
            "language": "en",
            "metadata": {},
        }

        try:
            response = requests.post(
                f"{self.base_url}/bookings",
                params={"apiKey": self.api_key},
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data", body) if isinstance(body, dict) else {}
            uid = data.get("uid")
            meeting_url = data.get("videoCallUrl") or data.get("bookingUrl") or data.get("location") or (
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
            elif any(isinstance(value, list) for value in data.values()):
                slot_maps.append(data)

        for slot_map in slot_maps:
            for day in sorted(slot_map.keys()):
                entries = slot_map.get(day) or []
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
