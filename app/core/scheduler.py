"""
Scheduled dispatch engine.
Resolves prospect timezone and calculates the next available send slot
within the allowed delivery windows, staggered across a user's queued sends.
"""
import datetime
from datetime import time, timedelta, timezone

import pytz
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.logging_config import logger

UTC = timezone.utc

# Delivery windows expressed in prospect local time
SEND_WINDOWS: list[tuple[time, time]] = [
    (time(9, 30), time(11, 59)),
    (time(13, 30), time(16, 0)),
]

STAGGER_MINUTES = 3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_prospect_timezone(db: Session, dm) -> str:
    """
    Returns a valid pytz timezone string for a DecisionMaker.
    Resolution order (DB-first, no unnecessary external calls):
      1. dm.display_timezone — trusted only when it is a real non-UTC timezone.
         A stored value of "UTC" is treated as a possibly-unresolved fallback and
         re-evaluated if the DM has any location hint (so a previous geopy timeout
         or network hiccup doesn't permanently strand the DM on UTC).
      2. Another DM at the same target company that has a real timezone resolved
      3. Another DM in the DB with the same location string that has a real timezone
      4. Geopy geocoding — only when the DB has zero prior record for this location
      5. UTC fallback — permanently cached only for DMs with no location hint
         (those will never resolve); DMs with a location return UTC transiently
         so the next call can retry geopy.
    """
    from app.db import models

    # Resolve the location hint upfront — needed for both the short-circuit
    # decision in step 1 and the geopy call in step 4.
    location = dm.location or (
        dm.target_company.location if dm.target_company else None
    )

    def _save_and_return(tz: str) -> str:
        """Persist the resolved timezone and return it."""
        dm.display_timezone = tz
        try:
            db.flush()
        except Exception:
            pass
        return tz

    # 1. Trust any stored non-UTC timezone unconditionally — it was resolved
    #    by a previous geopy call and is known-good.
    #    "UTC" is only trusted when the DM has NO location hint, because in that
    #    case geopy can never improve on it. If a location exists, "UTC" may be a
    #    stale failed-lookup fallback, so we fall through and retry steps 2-4.
    if dm.display_timezone is not None and dm.display_timezone != "UTC":
        if _is_valid_tz(dm.display_timezone):
            return dm.display_timezone

    if dm.display_timezone == "UTC" and not location:
        # No location → will never resolve; honour the cached fallback.
        return "UTC"

    # 2. Same target company — sibling DM may already have a real timezone.
    #    Exclude "UTC" so we don't propagate an unresolved fallback.
    if dm.target_company_id:
        row = (
            db.query(models.DecisionMaker.display_timezone)
            .filter(
                models.DecisionMaker.target_company_id == dm.target_company_id,
                models.DecisionMaker.id != dm.id,
                models.DecisionMaker.display_timezone.isnot(None),
                models.DecisionMaker.display_timezone != "UTC",
            )
            .first()
        )
        if row and row[0] and _is_valid_tz(row[0]):
            return _save_and_return(row[0])

    # 3. Same location string anywhere in the DB.
    if location:
        row = (
            db.query(models.DecisionMaker.display_timezone)
            .filter(
                models.DecisionMaker.location == location,
                models.DecisionMaker.id != dm.id,
                models.DecisionMaker.display_timezone.isnot(None),
                models.DecisionMaker.display_timezone != "UTC",
            )
            .first()
        )
        if row and row[0] and _is_valid_tz(row[0]):
            return _save_and_return(row[0])

    # 4. Geopy geocoding — only reached when the DB has no prior record.
    if location:
        tz = _lookup_timezone_from_location(location)
        if tz:
            return _save_and_return(tz)

    # 5. UTC fallback.
    #    • No location → cache it; geopy can never help here.
    #    • Has location but geopy failed this time → return UTC transiently
    #      WITHOUT saving, so the next dispatch attempt retries geopy.
    if not location:
        return _save_and_return("UTC")

    logger.warning(
        f"[SCHEDULER] Could not resolve timezone for DM {dm.id} "
        f"(location='{location}') — returning UTC transiently, will retry."
    )
    return "UTC"


def calculate_next_send_slot(user_id: str, prospect_tz: str, db: Session) -> datetime.datetime:
    """
    Returns a UTC-naive datetime for the next available send slot that:
    - Falls within a delivery window in prospect_tz
    - Is at least STAGGER_MINUTES after the latest queued send for user_id
    - Skips Saturday / Sunday
    """
    from app.db import models

    try:
        tz = pytz.timezone(prospect_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    now_utc = datetime.datetime.now(UTC)

    # Latest scheduled_at across user-owned EmailDrafts still pending
    latest_draft = (
        db.query(func.max(models.EmailDraft.scheduled_at))
        .join(models.Campaign)
        .filter(
            models.Campaign.user_id == user_id,
            models.EmailDraft.scheduled_at.isnot(None),
            models.EmailDraft.dispatch_state.in_(["QUEUED", "IN_PROGRESS"]),
        )
        .scalar()
    )

    # Latest scheduled_at across user-owned OutboundDispatches still pending
    latest_nudge = (
        db.query(func.max(models.OutboundDispatch.scheduled_at))
        .join(models.Campaign)
        .filter(
            models.Campaign.user_id == user_id,
            models.OutboundDispatch.scheduled_at.isnot(None),
            models.OutboundDispatch.state.in_(["QUEUED", "IN_PROGRESS"]),
        )
        .scalar()
    )

    candidates = [s for s in [latest_draft, latest_nudge] if s is not None]
    if candidates:
        latest_utc = max(candidates).replace(tzinfo=UTC)
        start_utc = max(now_utc, latest_utc + timedelta(minutes=STAGGER_MINUTES))
    else:
        start_utc = now_utc + timedelta(seconds=10)

    return _find_next_window_slot(start_utc, tz)


def is_in_delivery_window(now_utc: datetime.datetime, prospect_tz: str) -> bool:
    """
    Returns True if now_utc falls inside any delivery window in the prospect's
    local timezone AND is a weekday.  Used by the outbound worker to decide
    whether to send immediately or reschedule.
    """
    try:
        tz = pytz.timezone(prospect_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    # Attach UTC tzinfo if the caller passed a naive datetime
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)

    local = now_utc.astimezone(tz)

    if local.weekday() >= 5:           # Saturday / Sunday
        return False

    c_time = local.time().replace(second=0, microsecond=0)
    return any(win_start <= c_time <= win_end for win_start, win_end in SEND_WINDOWS)


def format_scheduled_display(slot_utc_naive: datetime.datetime, prospect_tz: str) -> str:
    """Returns a human-readable string like 'Mon 26 May at 9:30 AM IST'."""
    try:
        tz = pytz.timezone(prospect_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    local = slot_utc_naive.replace(tzinfo=UTC).astimezone(tz)
    abbr = local.strftime("%Z")
    # %-I is Linux-only; strip the leading zero manually for Windows compatibility
    hour = local.strftime("%I").lstrip("0") or "12"
    return local.strftime(f"%a %d %b at {hour}:%M %p {abbr}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_valid_tz(tz_str: str) -> bool:
    try:
        pytz.timezone(tz_str)
        return True
    except pytz.UnknownTimeZoneError:
        return False


# Module-level singletons — loaded once per worker process.
# TimezoneFinder loads a ~20 MB binary; Nominatim keeps a session open.
# Both are safe to share across threads (read-only after init).
_tf = None
_geocoder = None


def _get_timezonefinder():
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder
        _tf = TimezoneFinder()
    return _tf


def _get_geocoder():
    global _geocoder
    if _geocoder is None:
        from geopy.geocoders import Nominatim
        _geocoder = Nominatim(user_agent="ai-priori-scheduler", timeout=5)
    return _geocoder


def _lookup_timezone_from_location(location_str: str) -> str | None:
    try:
        geo = _get_geocoder().geocode(location_str)
        if not geo:
            return None

        tz = _get_timezonefinder().timezone_at(lng=geo.longitude, lat=geo.latitude)
        return tz
    except Exception as exc:
        logger.warning(f"[SCHEDULER] Timezone lookup failed for '{location_str}': {exc}")
        return None


def _find_next_window_slot(
    start_utc: datetime.datetime, tz: pytz.BaseTzInfo
) -> datetime.datetime:
    """
    Walks forward from start_utc until a slot inside a delivery window is found.
    Returns UTC-naive datetime.
    """
    candidate = start_utc.astimezone(tz)

    for _ in range(20):
        # Skip weekends — push to next Monday 9:30 AM
        while candidate.weekday() >= 5:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )

        c_time = candidate.time().replace(second=0, microsecond=0)

        for win_start, win_end in SEND_WINDOWS:
            if win_start <= c_time <= win_end:
                # Inside a valid window — done
                return candidate.astimezone(pytz.UTC).replace(tzinfo=None)
            if c_time < win_start:
                # Before this window — snap to its start and re-enter loop
                candidate = candidate.replace(
                    hour=win_start.hour, minute=win_start.minute,
                    second=0, microsecond=0,
                )
                break
        else:
            # Past all windows today — try first window next day
            candidate = (candidate + timedelta(days=1)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )

    # Safety fallback
    return (start_utc + timedelta(minutes=1)).replace(tzinfo=None)
