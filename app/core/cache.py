"""Redis read-cache for hot, expensive read paths.

Redis here is ONLY a cache, never a source of truth:
  * Every entry has a TTL.
  * Any Redis failure degrades silently to a cache MISS — the caller then reads
    Postgres exactly as it does today, so the app is fully correct with Redis off.

Campaign-detail invalidation uses a per-campaign GENERATION counter. The cache key
embeds the current generation; any write to a campaign-scoped row bumps that
generation (via the SQLAlchemy commit listener in app.db.database). The next read
computes a new key -> miss -> fresh rebuild, and the orphaned old entry expires by
TTL. This keeps cached data accurate WITHOUT having to clear the cache at every
write call-site — and over-bumping (e.g. during active stage processing) only costs
a harmless rebuild, never staleness.
"""
from __future__ import annotations

from app.core.logging_config import logger
from app.core.security import _get_redis

# Detail payload TTL — safety net behind generation invalidation. Also the de-facto
# max staleness for the rare case where a generation bump silently fails. Kept long
# because the generation counter already invalidates on every real write, so a high
# TTL only saves Neon reads (fewer rebuilds for an idle, open campaign page) without
# risking staleness.
CAMPAIGN_DETAIL_TTL = 300
_GEN_TTL = 7 * 24 * 3600  # abandoned campaigns' gen keys self-expire after a week

# Reused client: _get_redis() builds a fresh client (and TLS connection) per call,
# which is fine for infrequent locks but wasteful on the hot poll path. Memoise one
# client and reset it on error so it reconnects.
_client = None


def _client_or_none():
    global _client
    if _client is None:
        _client = _get_redis()
    return _client


def _reset():
    global _client
    _client = None


def cache_get(key: str):
    """Return cached bytes or None (None = miss OR Redis unavailable)."""
    try:
        r = _client_or_none()
        return r.get(key) if r else None
    except Exception:
        _reset()
        return None


def cache_set(key: str, value: bytes, ttl: int) -> None:
    try:
        r = _client_or_none()
        if r:
            r.set(key, value, ex=ttl)
    except Exception:
        _reset()


def cache_delete(key: str) -> None:
    try:
        r = _client_or_none()
        if r:
            r.delete(key)
    except Exception:
        _reset()


def campaign_generation(campaign_id: str) -> int:
    try:
        r = _client_or_none()
        if not r:
            return 0
        v = r.get(f"campaign:gen:{campaign_id}")
        return int(v) if v else 0
    except Exception:
        _reset()
        return 0


def bump_campaign_generation(campaign_id: str) -> None:
    """Invalidate a campaign's cached detail by advancing its generation."""
    try:
        r = _client_or_none()
        if r:
            r.incr(f"campaign:gen:{campaign_id}")
            r.expire(f"campaign:gen:{campaign_id}", _GEN_TTL)
    except Exception:
        _reset()


def campaign_detail_key(campaign_id: str) -> str:
    return f"campaign_detail:{campaign_id}:v{campaign_generation(campaign_id)}"


# --------------------------------------------------------------------------- #
# Geocode (location -> IANA timezone) cache                                   #
# A location's timezone is effectively permanent, so resolved hits live a long #
# time; unresolved misses are cached briefly so we re-try them occasionally    #
# instead of hammering the (rate-limited) geocoder on every run.              #
# --------------------------------------------------------------------------- #
_GEO_PREFIX = "geo:tz:"
_GEO_MISS = b"__none__"
GEOCODE_TTL = 180 * 24 * 3600        # resolved timezone — keep ~6 months
GEOCODE_MISS_TTL = 7 * 24 * 3600     # unresolved — re-try after a week


def geocode_cached(location_key: str):
    """Return (hit, tz). hit=False -> not cached; hit=True & tz=None -> cached as unresolved."""
    try:
        r = _client_or_none()
        if not r:
            return (False, None)
        v = r.get(_GEO_PREFIX + location_key)
        if v is None:
            return (False, None)
        return (True, None if v == _GEO_MISS else v.decode())
    except Exception:
        _reset()
        return (False, None)


def geocode_store(location_key: str, tz: str | None) -> None:
    try:
        r = _client_or_none()
        if not r:
            return
        if tz:
            r.set(_GEO_PREFIX + location_key, tz, ex=GEOCODE_TTL)
        else:
            r.set(_GEO_PREFIX + location_key, _GEO_MISS, ex=GEOCODE_MISS_TTL)
    except Exception:
        _reset()
