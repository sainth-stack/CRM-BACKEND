import pytz
from dateutil import parser as date_parser

from app.core.logging_config import logger
from app.db import models
from app.integrations.cal import cal_provider
from app.integrations.hubspot import hubspot_provider
from app.workers.lifecycle import (
    hold_company_siblings,
    terminate_company_siblings,
    transition_prospect,
)

IST_TIMEZONE = "Asia/Kolkata"
TIMEZONE_ALIASES = {
    "IST": IST_TIMEZONE,
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "GMT": "UTC",
    "UTC": "UTC",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
}
LOCATION_TIMEZONE_HINTS = [
    (("india", "mumbai", "delhi", "bengaluru", "bangalore", "hyderabad", "chennai", "kolkata", "pune"), IST_TIMEZONE),
    (("singapore",), "Asia/Singapore"),
    (("dubai", "uae", "abu dhabi"), "Asia/Dubai"),
    (("london", "uk", "united kingdom", "england"), "Europe/London"),
    (("paris", "france"), "Europe/Paris"),
    (("berlin", "germany"), "Europe/Berlin"),
    (("sydney", "melbourne", "australia"), "Australia/Sydney"),
    (("tokyo", "japan"), "Asia/Tokyo"),
    (("shanghai", "beijing", "china"), "Asia/Shanghai"),
    (("toronto", "ottawa", "canada"), "America/Toronto"),
    (("vancouver",), "America/Vancouver"),
    (("california", "los angeles", "san francisco", "san diego", "seattle", "washington state", "portland", "las vegas"), "America/Los_Angeles"),
    (("denver", "colorado", "phoenix", "arizona", "utah"), "America/Denver"),
    (("chicago", "texas", "dallas", "houston", "austin", "minnesota", "wisconsin", "illinois"), "America/Chicago"),
    (("new york", "boston", "atlanta", "miami", "florida", "washington dc", "new jersey", "usa", "united states", "us"), "America/New_York"),
]


def _normalize_timezone_label(raw_timezone: str | None) -> str | None:
    if not raw_timezone:
        return None

    candidate = raw_timezone.strip()
    if not candidate:
        return None

    alias = TIMEZONE_ALIASES.get(candidate.upper())
    if alias:
        return alias

    if candidate in pytz.all_timezones_set:
        return candidate

    normalized = candidate.replace(" ", "_")
    if normalized in pytz.all_timezones_set:
        return normalized

    return None


def _resolve_timezone_from_location(location_context: str | None) -> str:
    normalized_location = (location_context or "").strip().lower()
    if not normalized_location:
        return IST_TIMEZONE

    for keywords, timezone_name in LOCATION_TIMEZONE_HINTS:
        if any(keyword in normalized_location for keyword in keywords):
            return timezone_name

    return IST_TIMEZONE


def _resolve_schedule_timezone(dm, extract: dict | None) -> str:
    explicit_timezone = _normalize_timezone_label((extract or {}).get("timezone"))
    if explicit_timezone:
        return explicit_timezone

    company_location = dm.target_company.location if dm.target_company else None
    return _resolve_timezone_from_location(company_location)


def _request_scheduling_clarification(db, dm, *, source: str):
    from app.workers.tasks.ghostwriter_worker import draft_followup_worker

    logger.info(
        f"[DISCOVERY] Scheduling reply for {dm.name} is missing date/time details. Requesting next-week availability."
    )

    dm.termination_reason = None
    dm.retry_after = None

    if dm.target_company:
        dm.target_company.status = "DISCOVERY_CALL"
        for sibling in hold_company_siblings(db, dm):
            hubspot_provider.update_lead_status(sibling.hubspot_id, "On Hold (Peer Engaged)")

    transition_prospect(
        db,
        dm,
        state=models.ProspectState.DISCOVERY_CALL,
        status="DISCOVERY_CALL",
        reason="SCHEDULING_CLARIFICATION_REQUESTED",
        actor="sentinel",
        metadata={"source": source, "source_state": dm.state.value if dm.state else None},
    )
    dm.next_action_at = None
    draft_followup_worker(dm.id, db=db, manual_scheduling=True)


def _process_booking(db, dm, extract):
    source_tz_str = _resolve_schedule_timezone(dm, extract)
    source_tz = pytz.timezone(source_tz_str)
    ist_tz = pytz.timezone(IST_TIMEZONE)

    try:
        naive_dt = date_parser.parse(f"{extract['date']} {extract['time']}")
        localized_dt = source_tz.localize(naive_dt)
        ist_dt = localized_dt.astimezone(ist_tz)
        utc_dt = ist_dt.astimezone(pytz.UTC)
        ist_iso = ist_dt.isoformat()
        dm.scheduling_note = (
            f"Scheduling reply resolved in {source_tz_str} and converted to {IST_TIMEZONE} for booking."
        )
    except Exception:
        _request_scheduling_clarification(db, dm, source="date_time_parse_recheck")
        return

    user = dm.campaign.owner
    try:
        booking = cal_provider.book_meeting(
            db=db,
            user=user,
            email=dm.email,
            name=dm.name,
            start_time=ist_iso,
            booking_timezone=IST_TIMEZONE,
        )
    except Exception as e:
        logger.error(f"[DISCOVERY] Cal.com booking failed with exception: {e}", exc_info=True)
        booking = None
    if booking:
        transition_prospect(
            db,
            dm,
            state=models.ProspectState.MEETING_BOOKED,
            status="MEETING_BOOKED",
            reason="BOOKED",
            actor="sentinel",
            metadata={"source": "schedule_reply"},
        )
        dm.termination_reason = None
        dm.retry_after = None
        dm.meeting_link = booking["link"]
        dm.scheduled_time_utc = utc_dt.replace(tzinfo=None)
        dm.display_timezone = IST_TIMEZONE
        dm.next_action_at = None
        if dm.target_company:
            dm.target_company.status = "MEETING_BOOKED"
        for sibling in terminate_company_siblings(db, dm, actor="sentinel"):
            hubspot_provider.update_lead_status(sibling.hubspot_id, "Terminated (Internal Lead Secured)")
        hubspot_provider.update_lead_status(dm.hubspot_id, "Meeting Booked")
    else:
        _request_scheduling_clarification(db, dm, source="booking_retry_required")
