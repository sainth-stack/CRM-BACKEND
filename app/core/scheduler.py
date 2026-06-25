"""
Scheduled dispatch engine.
Resolves prospect timezone and calculates the next available send slot
within the allowed delivery windows, staggered across a user's queued sends.
"""
import datetime
import re
from datetime import time, timedelta, timezone

import pytz
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_config import logger

UTC = timezone.utc


def _parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


# Delivery windows expressed in prospect local time — driven by config so
# you can adjust business hours without a code deploy.
SEND_WINDOWS: list[tuple[time, time]] = [
    (_parse_hhmm(settings.SEND_WINDOW_MORNING_START),   _parse_hhmm(settings.SEND_WINDOW_MORNING_END)),
    (_parse_hhmm(settings.SEND_WINDOW_AFTERNOON_START), _parse_hhmm(settings.SEND_WINDOW_AFTERNOON_END)),
]

STAGGER_MINUTES = settings.SEND_STAGGER_MINUTES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Offline timezone maps — no network, no rate-limiting.
# Single-timezone countries resolve here instantly and exactly.
# Multi-timezone countries (US / CA / AU) are handled by their state/province
# maps below; a bare country name with no state stays None → geopy.
# ---------------------------------------------------------------------------

_COUNTRY_TZ = {
    # ── Europe ───────────────────────────────────────────────────────────────
    "united kingdom": "Europe/London", "uk": "Europe/London",
    "england": "Europe/London", "scotland": "Europe/London",
    "wales": "Europe/London", "northern ireland": "Europe/London",
    "ireland": "Europe/Dublin", "republic of ireland": "Europe/Dublin",
    "france": "Europe/Paris", "germany": "Europe/Berlin",
    "spain": "Europe/Madrid", "italy": "Europe/Rome",
    "netherlands": "Europe/Amsterdam", "holland": "Europe/Amsterdam",
    "belgium": "Europe/Brussels", "switzerland": "Europe/Zurich",
    "austria": "Europe/Vienna", "sweden": "Europe/Stockholm",
    "norway": "Europe/Oslo", "denmark": "Europe/Copenhagen",
    "finland": "Europe/Helsinki", "poland": "Europe/Warsaw",
    "portugal": "Europe/Lisbon", "czechia": "Europe/Prague",
    "czech republic": "Europe/Prague", "slovakia": "Europe/Bratislava",
    "hungary": "Europe/Budapest", "romania": "Europe/Bucharest",
    "bulgaria": "Europe/Sofia", "greece": "Europe/Athens",
    "croatia": "Europe/Zagreb", "slovenia": "Europe/Ljubljana",
    "serbia": "Europe/Belgrade", "bosnia": "Europe/Sarajevo",
    "bosnia and herzegovina": "Europe/Sarajevo",
    "montenegro": "Europe/Podgorica", "albania": "Europe/Tirane",
    "north macedonia": "Europe/Skopje", "macedonia": "Europe/Skopje",
    "moldova": "Europe/Chisinau", "ukraine": "Europe/Kiev",
    "belarus": "Europe/Minsk", "lithuania": "Europe/Vilnius",
    "latvia": "Europe/Riga", "estonia": "Europe/Tallinn",
    "luxembourg": "Europe/Luxembourg", "iceland": "Atlantic/Reykjavik",
    "malta": "Europe/Malta", "cyprus": "Asia/Nicosia",
    "turkey": "Europe/Istanbul", "turkiye": "Europe/Istanbul",
    # ── Middle East ──────────────────────────────────────────────────────────
    "uae": "Asia/Dubai", "united arab emirates": "Asia/Dubai",
    "saudi arabia": "Asia/Riyadh", "ksa": "Asia/Riyadh",
    "israel": "Asia/Jerusalem", "iraq": "Asia/Baghdad",
    "jordan": "Asia/Amman", "lebanon": "Asia/Beirut",
    "kuwait": "Asia/Kuwait", "bahrain": "Asia/Bahrain",
    "qatar": "Asia/Qatar", "oman": "Asia/Muscat",
    "yemen": "Asia/Aden", "iran": "Asia/Tehran",
    "syria": "Asia/Damascus",
    # ── Asia ─────────────────────────────────────────────────────────────────
    "india": "Asia/Kolkata", "singapore": "Asia/Singapore",
    "japan": "Asia/Tokyo", "south korea": "Asia/Seoul", "korea": "Asia/Seoul",
    "china": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong",
    "taiwan": "Asia/Taipei", "thailand": "Asia/Bangkok",
    "vietnam": "Asia/Ho_Chi_Minh", "viet nam": "Asia/Ho_Chi_Minh",
    "malaysia": "Asia/Kuala_Lumpur", "philippines": "Asia/Manila",
    "myanmar": "Asia/Rangoon", "burma": "Asia/Rangoon",
    "cambodia": "Asia/Phnom_Penh", "laos": "Asia/Vientiane",
    "nepal": "Asia/Kathmandu", "sri lanka": "Asia/Colombo",
    "bangladesh": "Asia/Dhaka", "pakistan": "Asia/Karachi",
    "afghanistan": "Asia/Kabul", "mongolia": "Asia/Ulaanbaatar",
    "uzbekistan": "Asia/Tashkent", "tajikistan": "Asia/Dushanbe",
    "turkmenistan": "Asia/Ashgabat", "kyrgyzstan": "Asia/Bishkek",
    "armenia": "Asia/Yerevan", "azerbaijan": "Asia/Baku",
    # "georgia" intentionally absent — ambiguous with the US state.
    # Resolved by city-level disambiguation in _offline_tz_from_location.
    # ── Africa ───────────────────────────────────────────────────────────────
    "south africa": "Africa/Johannesburg",
    "nigeria": "Africa/Lagos", "ghana": "Africa/Accra",
    "kenya": "Africa/Nairobi", "ethiopia": "Africa/Addis_Ababa",
    "tanzania": "Africa/Dar_es_Salaam", "uganda": "Africa/Kampala",
    "rwanda": "Africa/Kigali", "egypt": "Africa/Cairo",
    "morocco": "Africa/Casablanca", "algeria": "Africa/Algiers",
    "tunisia": "Africa/Tunis", "libya": "Africa/Tripoli",
    "sudan": "Africa/Khartoum", "senegal": "Africa/Dakar",
    "ivory coast": "Africa/Abidjan", "cote d ivoire": "Africa/Abidjan",
    "cameroon": "Africa/Douala", "angola": "Africa/Luanda",
    "mozambique": "Africa/Maputo", "zambia": "Africa/Lusaka",
    "zimbabwe": "Africa/Harare", "mali": "Africa/Bamako",
    "namibia": "Africa/Windhoek",
    # ── Americas (single-timezone) ───────────────────────────────────────────
    "argentina": "America/Argentina/Buenos_Aires",
    "colombia": "America/Bogota", "peru": "America/Lima",
    "venezuela": "America/Caracas", "ecuador": "America/Guayaquil",
    "bolivia": "America/La_Paz", "uruguay": "America/Montevideo",
    "paraguay": "America/Asuncion", "panama": "America/Panama",
    "costa rica": "America/Costa_Rica", "guatemala": "America/Guatemala",
    "el salvador": "America/El_Salvador", "honduras": "America/Tegucigalpa",
    "nicaragua": "America/Managua", "cuba": "America/Havana",
    "jamaica": "America/Jamaica",
    "trinidad": "America/Port_of_Spain", "trinidad and tobago": "America/Port_of_Spain",
    "barbados": "America/Barbados", "guyana": "America/Guyana",
    "suriname": "America/Paramaribo", "haiti": "America/Port-au-Prince",
    "dominican republic": "America/Santo_Domingo",
    "puerto rico": "America/Puerto_Rico",
    # Brazil spans 4 zones; São Paulo / BRT covers ~95 % of B2B contacts.
    # A bare "Brazil" resolves here; city-level strings fall to geopy for precision.
    "brazil": "America/Sao_Paulo", "brasil": "America/Sao_Paulo",
    # Chile has a single mainland zone (Easter Island is tiny/irrelevant for B2B).
    "chile": "America/Santiago",
    # Mexico spans 4 zones but Central time covers Mexico City + most business.
    # State-level strings in mx_city_tz fall to geopy for precision.
    "mexico": "America/Mexico_City",
    # ── Oceania ──────────────────────────────────────────────────────────────
    "new zealand": "Pacific/Auckland", "fiji": "Pacific/Fiji",
    "papua new guinea": "Pacific/Port_Moresby",
    # Australia is multi-zone — handled by _AU_STATE_TZ below.
    # Canada is multi-zone — handled by _CA_PROVINCE_TZ below.
}

# US state → tz (dominant zone; split-state edge cases use their majority zone).
_US_STATE_TZ = {
    "california": "America/Los_Angeles", "washington": "America/Los_Angeles",
    "oregon": "America/Los_Angeles", "nevada": "America/Los_Angeles",
    "arizona": "America/Phoenix",
    "utah": "America/Denver", "colorado": "America/Denver",
    "new mexico": "America/Denver", "montana": "America/Denver",
    "wyoming": "America/Denver", "idaho": "America/Denver",
    "texas": "America/Chicago", "illinois": "America/Chicago",
    "minnesota": "America/Chicago", "wisconsin": "America/Chicago",
    "iowa": "America/Chicago", "missouri": "America/Chicago",
    "arkansas": "America/Chicago", "louisiana": "America/Chicago",
    "oklahoma": "America/Chicago", "kansas": "America/Chicago",
    "nebraska": "America/Chicago", "mississippi": "America/Chicago",
    "alabama": "America/Chicago", "tennessee": "America/Chicago",
    "north dakota": "America/Chicago", "south dakota": "America/Chicago",
    "new york": "America/New_York", "new jersey": "America/New_York",
    "pennsylvania": "America/New_York", "massachusetts": "America/New_York",
    "connecticut": "America/New_York", "rhode island": "America/New_York",
    "vermont": "America/New_York", "new hampshire": "America/New_York",
    "maine": "America/New_York", "ohio": "America/New_York",
    "michigan": "America/New_York", "indiana": "America/New_York",
    "kentucky": "America/New_York", "virginia": "America/New_York",
    "west virginia": "America/New_York", "north carolina": "America/New_York",
    "south carolina": "America/New_York", "georgia": "America/New_York",
    "florida": "America/New_York", "maryland": "America/New_York",
    "delaware": "America/New_York", "district of columbia": "America/New_York",
    "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
}

# Canadian province → tz
_CA_PROVINCE_TZ = {
    "british columbia": "America/Vancouver", "bc": "America/Vancouver",
    "alberta": "America/Edmonton", "ab": "America/Edmonton",
    "saskatchewan": "America/Regina", "sk": "America/Regina",
    "manitoba": "America/Winnipeg", "mb": "America/Winnipeg",
    "ontario": "America/Toronto", "on": "America/Toronto",
    "quebec": "America/Toronto", "qc": "America/Toronto",
    "new brunswick": "America/Moncton", "nb": "America/Moncton",
    "nova scotia": "America/Halifax", "ns": "America/Halifax",
    "prince edward island": "America/Halifax", "pei": "America/Halifax",
    "newfoundland": "America/St_Johns", "nl": "America/St_Johns",
}

# Australian state → tz
_AU_STATE_TZ = {
    "new south wales": "Australia/Sydney", "nsw": "Australia/Sydney",
    "victoria": "Australia/Melbourne", "vic": "Australia/Melbourne",
    "queensland": "Australia/Brisbane", "qld": "Australia/Brisbane",
    "south australia": "Australia/Adelaide", "sa": "Australia/Adelaide",
    "western australia": "Australia/Perth", "wa": "Australia/Perth",
    "tasmania": "Australia/Hobart", "tas": "Australia/Hobart",
    "northern territory": "Australia/Darwin", "nt": "Australia/Darwin",
    "australian capital territory": "Australia/Sydney", "act": "Australia/Sydney",
}


def _offline_tz_from_location(location: str) -> str | None:
    """Resolve an IANA timezone from a location string WITHOUT any network call.

    Checks multi-zone countries (US / CA / AU) against their state/province maps
    first, then falls through to the single-timezone country map. Returns None only
    when the string cannot be placed confidently so the caller can try geopy."""
    if not location:
        return None
    t = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", location.lower())) + " "

    # US: check DC explicitly BEFORE the generic state loop — "washington" in
    # _US_STATE_TZ maps to America/Los_Angeles (WA state), which is wrong for DC.
    if " united states " in t or " usa " in t or " u s a " in t:
        if " washington dc " in t or " district of columbia " in t:
            return "America/New_York"
        for state, tz in _US_STATE_TZ.items():
            if f" {state} " in t:
                return tz
        return None

    # Canada: province match pins the zone; bare "Canada" stays None → geopy.
    if " canada " in t or " canadian " in t:
        for province, tz in _CA_PROVINCE_TZ.items():
            if f" {province} " in t:
                return tz
        return None

    # Australia: state match pins the zone; bare "Australia" stays None → geopy.
    if " australia " in t or " australian " in t:
        for state, tz in _AU_STATE_TZ.items():
            if f" {state} " in t:
                return tz
        return None

    # Georgia: "Georgia" is both a Caucasus country and a US state, so it cannot
    # be resolved by name alone. Disambiguate by city: known Georgian cities →
    # country; known GA cities → US state; anything else → geopy.
    if " georgia " in t:
        _GEO_COUNTRY_CITIES = {" tbilisi ", " batumi ", " kutaisi ", " rustavi ", " gori "}
        _GA_US_CITIES = {" atlanta ", " savannah ", " augusta ", " macon ", " athens ", " columbus "}
        if any(c in t for c in _GEO_COUNTRY_CITIES):
            return "Asia/Tbilisi"
        if any(c in t for c in _GA_US_CITIES):
            return "America/New_York"
        return None  # genuinely ambiguous → geopy

    # Single-timezone country map (covers the rest of the world).
    for country, tz in _COUNTRY_TZ.items():
        if f" {country} " in t:
            return tz
    return None


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

    # 3.5 OFFLINE country/state -> timezone (instant, no network). Resolves the common
    #     case (single-tz countries, US/CA/AU states) without touching the rate-limited
    #     geocoder, so the geopy ladder below is reached only for genuinely ambiguous
    #     strings. This is what keeps send-time resolution from re-introducing the wall
    #     that was removed from Stage 5.
    if location:
        off = _offline_tz_from_location(location)
        if off and _is_valid_tz(off):
            return _save_and_return(off)

    # 4. Geocoding (cleaned + laddered) — only reached when the DB has no record AND the
    #    offline map could not place the location.
    if location:
        tz = geocode_timezone(location)
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


def next_window_slot(start_utc: datetime.datetime, prospect_tz: str) -> datetime.datetime:
    """Public wrapper: next in-window, weekday-aligned slot at/after start_utc,
    in the prospect's timezone. Returns UTC-naive. Unlike calculate_next_send_slot
    this does NOT stagger behind other queued sends — it is used by the fire-time /
    recovery paths, where re-staggering behind the whole queue causes a cascade."""
    try:
        tz = pytz.timezone(prospect_tz)
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=UTC)
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


_geocode_fn = None


def _get_geocoder():
    global _geocoder
    if _geocoder is None:
        from geopy.geocoders import Nominatim
        _geocoder = Nominatim(user_agent="ai-priori-scheduler", timeout=5)
    return _geocoder


def _get_geocode_fn():
    """Rate-limited geocode callable — enforces Nominatim's ~1 req/sec usage
    policy so the deployment isn't blocked when geocoding many locations."""
    global _geocode_fn
    if _geocode_fn is None:
        from geopy.extra.rate_limiter import RateLimiter
        # Wrap a lambda (not the bound method) so the CURRENT _geocoder is resolved
        # on every call — preserves RateLimiter delay state while keeping the
        # geocoder swappable (tests, re-init).
        _geocode_fn = RateLimiter(
            lambda query, **kw: _get_geocoder().geocode(query, **kw),
            min_delay_seconds=1.0,
            max_retries=1,
            swallow_exceptions=True,
        )
    return _geocode_fn


# Postcode patterns stripped from location strings before geocoding — Nominatim
# resolves clean place names far better than jammed "town postcode" fragments.
_UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
_US_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")


def _normalize_location(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    # Collapse the redundant "United Kingdom Uk" the source data emits.
    s = re.sub(r"United Kingdom\s+Uk\b", "United Kingdom", s, flags=re.I)
    return s


def _location_candidates(location: str) -> list[str]:
    """Build progressively coarser geocode queries from a messy location string.

    e.g. 'England, nuneaton and bedworth cv11 5ab, United Kingdom Uk' ->
      ['CV11 5AB, United Kingdom', 'England, nuneaton and bedworth, United Kingdom',
       'nuneaton and bedworth, United Kingdom', 'England, United Kingdom',
       'United Kingdom']
    The first candidate that geocodes wins. Country-level still yields the correct
    timezone for single-tz countries (UK, Italy, ...) and is only a last resort.
    """
    s = _normalize_location(location)
    segs = [seg.strip(" ,") for seg in s.split(",") if seg.strip(" ,")]
    cleaned: list[str] = []
    for seg in segs:
        c = _US_ZIP.sub("", _UK_POSTCODE.sub("", seg))
        c = re.sub(r"\s+", " ", c).strip(" ,")
        if c:
            cleaned.append(c)

    cands: list[str] = []

    def add(c: str) -> None:
        if not c:
            return
        c = re.sub(r"\s+", " ", c).strip(" ,")
        if c and c.lower() not in [x.lower() for x in cands]:
            cands.append(c)

    pc = _UK_POSTCODE.search(s)
    if pc and cleaned:
        add(f"{pc.group(0)}, {cleaned[-1]}")          # precise: 'CV11 5AB, United Kingdom'
    add(", ".join(cleaned))                            # full cleaned
    if len(cleaned) >= 2:
        add(", ".join(cleaned[-2:]))                   # city/region + country
        add(f"{cleaned[0]}, {cleaned[-1]}")            # region + country
    if cleaned:
        add(cleaned[-1])                               # country (last resort)
    return cands


def _lookup_timezone_from_location(location_str: str) -> str | None:
    try:
        geo = _get_geocode_fn()(location_str)
        if not geo:
            return None
        tz = _get_timezonefinder().timezone_at(lng=geo.longitude, lat=geo.latitude)
        return tz
    except Exception as exc:
        logger.warning(f"[SCHEDULER] Timezone lookup failed for '{location_str}': {exc}")
        return None


def geocode_timezone(location: str | None, cache: dict | None = None) -> str | None:
    """Resolve an IANA timezone from a free-text location string.

    Tries a ladder of progressively coarser queries (see _location_candidates) so
    messy 'town postcode, Country' strings still resolve instead of falling back to
    UTC. An optional per-run cache (keyed by the raw location) ensures each distinct
    location is geocoded only once. Returns None only when nothing resolves.
    """
    if not location or not str(location).strip():
        return None
    key = str(location).strip().lower()
    if cache is not None and key in cache:
        return cache[key]

    # Cross-run/worker cache (Redis): a location's timezone is effectively permanent,
    # so this avoids re-hitting the rate-limited geocoder for locations seen before.
    from app.core.cache import geocode_cached, geocode_store
    hit, tz_cached = geocode_cached(key)
    if hit:
        if cache is not None:
            cache[key] = tz_cached
        return tz_cached

    result: str | None = None
    for candidate in _location_candidates(location):
        tz = _lookup_timezone_from_location(candidate)
        if tz and _is_valid_tz(tz):
            result = tz
            break

    geocode_store(key, result)
    if cache is not None:
        cache[key] = result
    return result


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
