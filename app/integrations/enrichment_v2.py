"""
enrichment_v2.py — Multi-layer company enrichment (v2).

Six free/open-source data sources per company:
  L1  Company website + tech fingerprint + owned RSS + press pages (via extract_site)
  L2  Google News RSS (date-filtered, newest-first, age-capped to MAX_AGE_MONTHS)
  L3  DDGS web search (past year, 4 parallel queries, globally throttled to 4 concurrent)
  L4  Live hiring (Greenhouse/Lever JSON boards + company careers page fallback)
  L5  Wikipedia plain-text summary
  L6  Targeted pain-signal search (optional — uses caller-supplied search_hints derived
      from the sender's capability_to_pain_map; runs only when hints are provided)

A single LLM call fuses all 5 sources into a structured CompanyEnrichment record.
Every layer is non-fatal: a failed source returns empty, never raises. A degraded
record (tech fingerprint + site snippet) is always returned when the LLM fails.

MEMORY CONTRACT (enforced here, not by callers):
  - The caller MUST supply a shared curl_cffi AsyncSession via the `client` kwarg.
    At 20 concurrent companies this is the single most important RAM control: one
    shared session with max_clients=30 uses ~20-30 MB total vs 20 × individual
    sessions that would consume 300-600 MB.
  - Trafilatura/lxml is bounded to TRAFILATURA_CONCURRENCY=8 simultaneous parses
    via a lazy semaphore. Each lxml parse holds ~3-8 MB; without bounding, 20
    concurrent companies × 10 pages each = up to 200 simultaneous parses in the
    thread pool.
  - HTML fetch caps are set conservatively (60 KB per press page, 100 KB for RSS).
    Most useful content lives in the first 60 KB; higher caps waste RAM in buffers.
  - DDGS is globally throttled to DDGS_CONCURRENCY=4 concurrent calls (shared
    across all campaigns in the process).

Usage (from Stage 3):
    from curl_cffi.requests import AsyncSession
    from app.integrations.enrichment_v2 import enrich_company

    async with AsyncSession(impersonate="chrome", max_clients=30) as client:
        results = await asyncio.gather(*[
            enrich_company(name, domain, client=client)
            for name, domain in batch
        ])
"""
from __future__ import annotations

import asyncio
import os
import re
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.logging_config import logger

# =============================================================================
# CONFIG
# =============================================================================
SITE_CHAR_BUDGET        = 12_000
PRESS_CHAR_BUDGET       = 6_000
NEWS_CHAR_BUDGET        = 2_500
DDGS_CHAR_BUDGET        = 4_500
HIRING_CHAR_BUDGET      = 2_000
WIKI_CHAR_BUDGET        = 1_500
PAIN_SIGNALS_CHAR_BUDGET = 4_000   # L6 — targeted pain-signal search results

HARD_TIMEOUT_SEC        = int(os.getenv("ENRICH_TIMEOUT_SEC", "75"))     # per-company watchdog (seconds)
DDGS_CONCURRENCY        = int(os.getenv("ENRICH_DDGS_CONCURRENCY", "4"))  # max concurrent DDGS thread-pool calls (global)
NEWS_CONCURRENCY        = int(os.getenv("ENRICH_NEWS_CONCURRENCY", "8"))  # max concurrent Google News fetches (global)
# Global cap on simultaneous lxml/trafilatura parses. This is the single heaviest
# transient RAM allocation in the pipeline (each parse builds a full DOM tree), so it
# is the main knob for capping peak memory INDEPENDENT of company concurrency. Kept
# below ICP_CONCURRENCY on purpose: at 20 concurrent companies only 6 parse at once.
TRAFILATURA_CONCURRENCY = int(os.getenv("ENRICH_TRAFILATURA_CONCURRENCY", "6"))
MAX_AGE_MONTHS          = int(os.getenv("ENRICH_NEWS_MAX_AGE_MONTHS", "12"))  # drop news older than this

_CUR_YEAR           = datetime.now().year
_YEAR_TERMS         = f"{_CUR_YEAR} {_CUR_YEAR - 1}"
_SIGNAL_CUTOFF_YEAR = _CUR_YEAR - 1   # events older than this → company_history

# Press/signal paths — crawled with normal timeout + retry
# Certifications/quality pages are high-value for ICP (AS9100, ISO, IATF etc) — kept first.
_SIGNAL_PATHS      = ("certifications", "quality", "accreditations", "news", "newsroom", "press", "press-releases", "blog", "media")
# Enrichment paths — crawled on best-effort with short timeout (only 30-40% hit)
_ENRICHMENT_PATHS  = ("customers", "case-studies", "partners", "about")
# Careers paths for ATS fallback scrape
_CAREERS_PATHS     = ("careers", "jobs", "career", "join-us", "company/careers")

# HTML fetch caps (per page).  60 KB is sufficient for trafilatura content
# extraction and tech fingerprinting; higher caps only waste RAM in buffers.
_HTML_CAP          = 60_000    # press / careers / homepage pages
_RSS_CAP           = 100_000   # RSS feed XML (more items, but simpler text)
_WIKI_CAP          = 20_000    # Wikipedia API response (JSON)

# =============================================================================
# Lazy global throttle semaphores — created inside the running event loop
# so they are safe across hot-reloads and multiple campaigns in one process.
# _throttles_lock guards the check-then-set: without it two coroutines that
# both see a missing key simultaneously create two separate semaphores and the
# second write silently discards the first, allowing 2× the intended concurrency
# on the very first Stage 3 run (e.g. 8 DDGS calls instead of 4).
# =============================================================================
_throttles: dict[str, asyncio.Semaphore] = {}
_throttles_lock: asyncio.Lock | None = None


def _get_throttles_lock() -> asyncio.Lock:
    global _throttles_lock
    if _throttles_lock is None:
        _throttles_lock = asyncio.Lock()
    return _throttles_lock


async def _sem_async(name: str, n: int) -> asyncio.Semaphore:
    """Return (or lazily create) the named semaphore — race-free."""
    s = _throttles.get(name)
    if s is not None:
        return s
    async with _get_throttles_lock():
        s = _throttles.get(name)   # re-check inside lock
        if s is None:
            s = asyncio.Semaphore(n)
            _throttles[name] = s
    return s


def _sem(name: str, n: int) -> asyncio.Semaphore:
    """Sync accessor — safe to call when the semaphore is already initialised.
    Use _sem_async on first access from async context to avoid the race."""
    s = _throttles.get(name)
    if s is None:
        s = asyncio.Semaphore(n)
        _throttles[name] = s
    return s


# =============================================================================
# Utilities
# =============================================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.strip()[:25], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


async def _retry(factory, *, attempts: int = 3, base: float = 0.6):
    import random
    last = None
    for i in range(attempts):
        try:
            return await factory()
        except Exception as e:
            last = e
            if i < attempts - 1:
                await asyncio.sleep(base * (2 ** i) + random.random() * 0.4)
    raise last


# =============================================================================
# Tech fingerprint (HTML source scan)
# =============================================================================
_TECH_SIGS: list[tuple[str, list[str]]] = [
    ("HubSpot",            ["hubspot.com", "hs-scripts", "hsforms"]),
    ("Salesforce",         ["salesforce.com", "force.com", "pardot"]),
    ("Marketo",            ["marketo.net", "mktdns", "munchkin.js"]),
    ("Microsoft Dynamics", ["dynamics.com", "microsoftdynamics"]),
    ("SAP",                ["sap.com", "sap-ui5", "sapphirenow"]),
    ("Oracle",             ["oracle.com", "oraclecontent"]),
    ("ServiceNow",         ["servicenow.com", "service-now.com"]),
    ("Workday",            ["workday.com", "wd5.myworkday"]),
    ("AWS",                ["amazonaws.com", "cloudfront.net", "s3.amazonaws"]),
    ("Azure",              ["azureedge.net", "azure.microsoft", "azurewebsites"]),
    ("GCP",                ["googleapis.com", "storage.cloud.google"]),
    ("Google Analytics",   ["google-analytics.com", "gtag/js", "ga.js"]),
    ("Segment",            ["segment.com", "segment.io", "analytics.segment"]),
    ("Mixpanel",           ["mixpanel.com"]),
    ("Hotjar",             ["hotjar.com"]),
    ("Shopify",            ["shopify.com", "cdn.shopify"]),
    ("WordPress",          ["wp-content", "wp-includes"]),
    ("Intercom",           ["intercom.io", "intercom.com/js"]),
    ("Drift",              ["drift.com", "js.driftt.com"]),
    ("Zendesk",            ["zdassets.com", "zendesk.com"]),
    ("React",              ["react.development.js", "react.production.min"]),
    ("Angular",            ["angular.min.js", "angular/core"]),
    ("Vue",                ["vue.min.js", "vue@", "/vue/"]),
]


def _fingerprint_html(html: str) -> list[str]:
    low = html.lower()
    return [name for name, sigs in _TECH_SIGS if any(s.lower() in low for s in sigs)]


# =============================================================================
# Company name → meaningful tokens (for noise filtering in news/DDGS results)
# =============================================================================
_NAME_STOPWORDS = {
    "inc", "ltd", "llc", "corp", "corporation", "co", "company", "group",
    "holdings", "oyj", "plc", "gmbh", "ag", "sa", "nv", "bv", "the", "and",
    "international", "global", "solutions", "systems", "technologies",
}


def _name_tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", name.lower())
            if t not in _NAME_STOPWORDS and len(t) > 2]


def _mentions_company(text: str, name: str, tokens: list[str]) -> bool:
    low = text.lower()
    if name.lower() in low:
        return True
    if not tokens:
        return True

    def _has(tok: str) -> bool:
        return re.search(rf"\b{re.escape(tok)}\b", low) is not None

    if len(tokens) == 1:
        return _has(tokens[0])
    return all(_has(t) for t in tokens)


# =============================================================================
# Signal prefilter — keeps high-signal paragraphs within a char budget
# =============================================================================
_SIGNAL_KWS = {
    "fund", "raise", "invest", "series", "acqui", "merger", "expand",
    "launch", "partner", "contract", "revenue", "grow", "hire", "appoint",
    "leadership", "executive", "ceo", "cto", "president", "director",
    "employee", "headcount", "staff", "workforce", "million", "billion",
    "transform", "automat", "digital", "platform", "software", "cloud",
    "integrat", "customer", "solution", "product", "service", "market",
    "operat", "global", "founded", "headquarter", "office", "industr",
}


def _signal_score_para(para: str) -> int:
    low = para.lower()
    return sum(1 for kw in _SIGNAL_KWS if kw in low)


def _prefilter_enrichment(text: str, char_limit: int = SITE_CHAR_BUDGET) -> str:
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) > 30]
    if not paras:
        return text[:char_limit]
    lead_budget = int(char_limit * 0.6)
    lead, total, taken = [], 0, 0
    for p in paras:
        if total + len(p) > lead_budget:
            break
        lead.append(p); total += len(p); taken += 1
    for p in sorted(paras[taken:], key=_signal_score_para, reverse=True):
        if total + len(p) > char_limit:
            continue
        lead.append(p); total += len(p)
    return "\n\n".join(lead)


# =============================================================================
# CompanyEnrichment — the structured output schema
# =============================================================================
class CompanyEnrichment(BaseModel):
    # Primary ICP signals
    growth_signals: List[str] = Field(description="FACTUAL events only (no inferences): funding rounds, facility expansions, contract wins, product launches, acquisitions — each traceable to a DATED source within the last 12 months. Omit if the event cannot be dated or the date is before the cutoff year.")
    recent_events: List[str] = Field(description="FACTUAL events only (no inferences): leadership changes, M&A, partnerships, awards — each with an explicit date or clearly recent sourcing. Omit anything undated, evergreen, or older than 12 months.")
    hiring_signals: List[str] = Field(description="Active hiring: specific roles/departments/open-position counts. Empty if none.")
    pain_points: List[str] = Field(description="Operational/strategic challenges inferred from concrete signals (hiring gaps, scale pressure from funding, leadership churn, tech-debt indicators, customer complaints, regulatory exposure). Inference from indirect evidence is fine — grounding is required, platitudes are not.")
    # Context fields
    overview: str = Field(description="2-4 sentence summary of what the company does/offers. 'Unknown' if site gave nothing.")
    products_services: List[str] = Field(description="Concrete products/product lines/services. Be thorough.")
    target_customers: str = Field(description="Who the company sells to. 'Unknown' if not stated.")
    notable_customers_partners: List[str] = Field(description="Named customers/clients/partners.")
    technologies: List[str] = Field(description="Software platforms, cloud services, CRM/ERP systems USED to run the business. Not certifications.")
    certifications: List[str] = Field(description="Industry certifications, quality standards (ISO 9001, SOC 2, CE marking, IAPMO, LEED, etc.).")
    awards_recognition: List[str] = Field(description="Awards, rankings, or public recognition received.")
    key_initiatives: List[str] = Field(description="Stated strategic priorities or transformation programs.")
    company_history: List[str] = Field(description="Historical events OLDER than 1 year (founding, legacy M&A, old funding rounds). Background context only — not ICP signals.")


# =============================================================================
# Stale-signal filter: move pre-cutoff growth/events to company_history
# =============================================================================
_YEAR_RE = re.compile(r'\b((?:19|20)\d{2})\b')


def _has_recent_year(text: str) -> bool:
    return any(int(y) >= _SIGNAL_CUTOFF_YEAR for y in _YEAR_RE.findall(text))


def _dominant_year(text: str) -> Optional[int]:
    m = _YEAR_RE.search(text)
    return int(m.group(1)) if m else None


_UNKNOWN_DATE_RE = re.compile(
    r'\b(unknown|unspecified|undated|no[\s-]date|date\s*unknown|date\s*:\s*unknown)\b',
    re.IGNORECASE,
)


def _filter_stale_signals(rec: CompanyEnrichment) -> CompanyEnrichment:
    def _split(items: list[str]) -> tuple[list[str], list[str]]:
        keep, move = [], []
        for item in items:
            # Explicitly undated items — drop entirely (not even history-worthy).
            if _UNKNOWN_DATE_RE.search(item):
                continue
            if _has_recent_year(item):
                # Contains at least one year >= cutoff — treat as fresh.
                keep.append(item)
            else:
                # Either has an explicit old year OR no year at all.
                # Both cases → demote to company_history. growth_signals and
                # recent_events now require a dated source (per prompt rule 1b),
                # so an undated item that slipped through the LLM is stale by
                # definition and must not reach the email drafter.
                move.append(item)
        return keep, move

    fresh_growth, stale_growth = _split(rec.growth_signals)
    fresh_events, stale_events = _split(rec.recent_events)
    demoted = stale_growth + stale_events
    if demoted:
        existing = list(rec.company_history)
        rec.company_history = list(dict.fromkeys([*demoted, *existing]))
        rec.growth_signals  = fresh_growth
        rec.recent_events   = fresh_events
    return rec


# =============================================================================
# L1: Website + tech fingerprint + owned RSS + press pages
# =============================================================================
async def _fetch_html(client, url: str, *, cap: int = _HTML_CAP,
                      timeout: tuple = (3, 6), attempts: int = 2) -> str:
    """Fetch URL with cap. Returns '' on any failure — never raises."""
    async def _go():
        r = await client.get(url, timeout=timeout, allow_redirects=True)
        return r.text[:cap] if r.status_code < 400 else ""
    try:
        return await _retry(_go, attempts=attempts)
    except Exception:
        return ""


def _discover_rss(html: str, base: str) -> Optional[str]:
    m = re.search(
        r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*href=["\']([^"\']+)["\']',
        html, re.IGNORECASE)
    if not m:
        m = re.search(
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]*type=["\']application/(?:rss|atom)\+xml["\']',
            html, re.IGNORECASE)
    if not m:
        return None
    href = m.group(1)
    return href if href.startswith("http") else urllib.parse.urljoin(base, href)


def _parse_feed(xml: str, cutoff: datetime) -> tuple[list[str], Optional[datetime]]:
    def _tag(block, tag):
        mm = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL | re.IGNORECASE)
        if not mm:
            return ""
        v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", mm.group(1), flags=re.DOTALL)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v)).strip()

    blocks = re.findall(r"<(?:item|entry)>(.*?)</(?:item|entry)>", xml, re.DOTALL | re.IGNORECASE)
    dated, newest = [], None
    for b in blocks:
        title  = _tag(b, "title")
        date_s = _tag(b, "pubDate") or _tag(b, "published") or _tag(b, "updated")
        dt = _parse_dt(date_s)
        if not title:
            continue
        if dt and dt < cutoff:
            continue
        if dt and (newest is None or dt > newest):
            newest = dt
        dated.append((dt or datetime.min.replace(tzinfo=timezone.utc),
                      f"[{date_s[:16]}] {title}"))
    dated.sort(key=lambda x: x[0], reverse=True)
    return [line for _, line in dated[:6]], newest


async def _layer1_site_tech_rss(client, domain: str):
    """Returns (site_text, press_text, techs, owned_news_lines, owned_newest_dt)."""
    import trafilatura
    from app.integrations.site_extractor import extract_site

    base   = f"https://{domain}"
    cutoff = _now().replace(year=_now().year - max(1, MAX_AGE_MONTHS // 12))

    async def _fetch_press(path: str, *, timeout=(3, 6), attempts=2) -> str:
        html = await _fetch_html(client, f"{base}/{path}", timeout=timeout, attempts=attempts)
        if not html:
            return ""
        # Bound lxml memory: at most TRAFILATURA_CONCURRENCY parses simultaneously.
        async with _sem("trafilatura", TRAFILATURA_CONCURRENCY):
            try:
                txt = await asyncio.to_thread(
                    lambda h: trafilatura.extract(h, include_comments=False,
                                                  favor_recall=True) or "", html)
            except Exception:
                txt = ""
        html = None  # release buffer immediately after extraction
        return f"[/{path}]\n{txt[:2_500]}" if txt and len(txt) > 200 else ""

    try:
        main, html_raw, *press_parts = await asyncio.gather(
            # Pass shared client so extract_site doesn't create its own session.
            extract_site(domain, client=client, max_pages=5, per_page_chars=7_000, timeout=8.0),
            _fetch_html(client, base),
            # Core signal paths: normal timeout + retry.
            *[_fetch_press(p) for p in _SIGNAL_PATHS],
            # Enrichment paths: shorter timeout, no retry (absent on most sites).
            *[_fetch_press(p, timeout=(1, 3), attempts=1) for p in _ENRICHMENT_PATHS],
            return_exceptions=True,
        )
    except Exception:
        return "[CRAWL ERROR]", "", [], [], None

    if isinstance(main, Exception) or not getattr(main, "ok", False):
        site_text = "[CRAWL FAILED]"
    else:
        site_text = main.combined_text or ""

    html_raw   = html_raw if isinstance(html_raw, str) else ""
    press_text = "\n\n".join(p for p in press_parts if isinstance(p, str) and p)
    techs      = _fingerprint_html(html_raw) if html_raw else []

    # Company-owned RSS feed → dated announcements (freshest first-party signal).
    owned_news, newest = [], None
    rss_url = _discover_rss(html_raw, base) if html_raw else None
    html_raw = None  # release homepage buffer after fingerprinting + RSS discovery
    if rss_url:
        feed_xml = await _fetch_html(client, rss_url, cap=_RSS_CAP)
        if feed_xml:
            owned_news, newest = _parse_feed(feed_xml, cutoff)
        feed_xml = None

    return site_text, press_text, techs, owned_news, newest


# =============================================================================
# L2: Google News RSS (date-parsed, age-capped, newest-first)
# =============================================================================
async def _layer2_google_news(client, company_name: str, tokens: list[str]):
    cutoff = _now().replace(year=_now().year - max(1, MAX_AGE_MONTHS // 12))
    # Build an after: date string so Google News pre-filters at the query level.
    from datetime import timedelta
    after_date = (_now() - timedelta(days=MAX_AGE_MONTHS * 30)).strftime("%Y-%m-%d")
    try:
        async with _sem("news", NEWS_CONCURRENCY):
            query = urllib.parse.quote(f'"{company_name}" after:{after_date}')
            url   = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
            xml   = await _fetch_html(client, url, cap=_RSS_CAP * 4)

        def _tag(block, tag):
            mm = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.DOTALL | re.IGNORECASE)
            if not mm:
                return ""
            v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", mm.group(1), flags=re.DOTALL)
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", v)).strip()

        items  = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL | re.IGNORECASE)
        xml    = None  # release RSS XML buffer
        scored, newest = [], None
        for b in items:
            title  = _tag(b, "title")
            date_s = _tag(b, "pubDate")
            src    = _tag(b, "source")
            if not _mentions_company(title, company_name, tokens):
                continue
            dt = _parse_dt(date_s)
            # Strict gate: skip items with unparseable dates rather than blindly including
            # them (the previous behaviour let stale articles with bad date strings leak through).
            if dt is None:
                continue
            if dt < cutoff:
                continue
            if newest is None or dt > newest:
                newest = dt
            scored.append((dt, f"[{date_s[:16]}] {title}" + (f" | {src}" if src else "")))
        scored.sort(key=lambda x: x[0], reverse=True)
        lines = [l for _, l in scored[:10]]
        return "\n\n".join(lines) if lines else "[No fresh Google News]", newest
    except Exception as exc:
        return f"[Google News failed: {exc}]", None


# =============================================================================
# L3: DDGS — past year, 4 parallel queries, globally throttled + retried
#
# Runs in a thread executor (DDGS is sync).  Two groups (A, B) run concurrently
# so wall-time ≈ max(group_A, group_B) — same as running 2 queries.
# Global DDGS_CONCURRENCY=4 semaphore prevents the worker from hammering
# DuckDuckGo across multiple concurrent companies.
# =============================================================================
async def _layer3_ddgs(company_name: str, tokens: list[str],
                       site_unreachable: bool = False) -> str:
    _GROUP_A = [
        f'"{company_name}" (funding OR acquisition OR expansion OR contract OR launch) {_YEAR_TERMS}',
        f'"{company_name}" (hiring OR "is hiring" OR "open roles" OR "job opening") {_YEAR_TERMS}',
    ]
    _GROUP_B = [
        f'"{company_name}" (award OR recognition OR "case study" OR customer OR partnership) {_YEAR_TERMS}',
        f'"{company_name}" (challenge OR problem OR "pain point" OR review OR initiative OR strategy) {_YEAR_TERMS}',
    ]
    # Certification-specific query — reliably surfaces ISO/AS9100/IATF/CE where the
    # website is thin or unreachable. Proven in test runs to recover certs missed by crawl.
    _GROUP_C = [
        f'"{company_name}" (ISO OR AS9100 OR IATF OR CE OR certified OR accredited OR certification)',
    ]
    # Extra queries fired only when the company website is unreachable — compensates
    # for having no crawl data to feed the LLM.
    _GROUP_SITE_FALLBACK = [
        f'"{company_name}" company products services overview',
        f'"{company_name}" site:linkedin.com',
        f'"{company_name}" employees headquarters founded',
    ]

    def _search_group(queries):
        from ddgs import DDGS
        hits, seen = [], set()
        with DDGS() as d:
            for q in queries:
                try:
                    for h in d.text(q, timelimit="y", max_results=4):
                        u = h.get("href", "")
                        if u and u not in seen:
                            seen.add(u)
                            hits.append(h)
                except Exception:
                    continue
        return hits

    loop = asyncio.get_event_loop()

    async def _run_group(queries):
        async with _sem("ddgs", DDGS_CONCURRENCY):
            async def _go():
                return await loop.run_in_executor(None, _search_group, queries)
            try:
                return await _retry(_go, attempts=3, base=1.0)
            except Exception:
                return []

    try:
        groups = [_run_group(_GROUP_A), _run_group(_GROUP_B), _run_group(_GROUP_C)]
        if site_unreachable:
            groups.append(_run_group(_GROUP_SITE_FALLBACK))
        group_results = await asyncio.gather(*groups, return_exceptions=True)
        seen_urls: set[str] = set()
        all_hits: list[dict] = []
        for gr in group_results:
            if isinstance(gr, list):
                for h in gr:
                    u = h.get("href", "")
                    if u not in seen_urls:
                        seen_urls.add(u)
                        all_hits.append(h)
    except Exception as exc:
        return f"[DDGS failed: {exc}]"

    kept = []
    for h in all_hits:
        title = (h.get("title") or "").strip()
        body  = (h.get("body") or "").strip()
        if _mentions_company(f"{title} {body}", company_name, tokens):
            kept.append(f"- {title}\n  {body[:180]}")
    return "\n\n".join(kept) if kept else "[No on-target DDGS results]"


# =============================================================================
# L4: Hiring — Greenhouse/Lever JSON boards + careers page fallback
# =============================================================================
def _ats_slugs(name: str, domain: str) -> list[str]:
    dom = domain.split(".")[0]
    n = name.lower()
    cands = {
        re.sub(r"[^a-z0-9]+", "", n),
        re.sub(r"[^a-z0-9]+", "-", n).strip("-"),
        dom,
    }
    return [c for c in cands if c and len(c) > 2][:3]


async def _layer4_hiring(client, domain: str, name: str):
    """Returns (text, newest_dt)."""
    import trafilatura
    base   = f"https://{domain}"
    lines: list[str] = []
    newest: Optional[datetime] = None

    async def _greenhouse(slug):
        async def _go():
            r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                                 timeout=(3, 6), allow_redirects=True)
            return r.json() if r.status_code == 200 else None
        try:
            data = await _retry(_go, attempts=2)
            return (data or {}).get("jobs") or []
        except Exception:
            return []

    async def _lever(slug):
        async def _go():
            r = await client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                                 timeout=(3, 6), allow_redirects=True)
            return r.json() if r.status_code == 200 else None
        try:
            return await _retry(_go, attempts=2) or []
        except Exception:
            return []

    slugs = _ats_slugs(name, domain)
    gh_lists, lv_lists = await asyncio.gather(
        asyncio.gather(*[_greenhouse(s) for s in slugs]),
        asyncio.gather(*[_lever(s) for s in slugs]),
    )
    for jobs in gh_lists:
        for j in jobs[:12]:
            title = j.get("title") or ""
            loc   = ((j.get("location") or {}) or {}).get("name") or ""
            dt    = _parse_dt(j.get("updated_at") or j.get("first_published") or "")
            if dt and (newest is None or dt > newest):
                newest = dt
            if title:
                lines.append(f"- {title}" + (f" ({loc})" if loc else "") + " [Greenhouse]")
    for posts in lv_lists:
        for p in posts[:12]:
            title = p.get("text") or ""
            loc   = ((p.get("categories") or {}) or {}).get("location") or ""
            ts    = p.get("createdAt")
            dt    = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if isinstance(ts, (int, float)) else None
            if dt and (newest is None or dt > newest):
                newest = dt
            if title:
                lines.append(f"- {title}" + (f" ({loc})" if loc else "") + " [Lever]")

    # Fallback: scrape company careers page when ATS boards found nothing.
    if not lines:
        async def _fetch_careers(path):
            html = await _fetch_html(client, f"{base}/{path}")
            if not html:
                return ""
            async with _sem("trafilatura", TRAFILATURA_CONCURRENCY):
                try:
                    txt = await asyncio.to_thread(
                        lambda h: trafilatura.extract(h, include_comments=False,
                                                      favor_recall=True) or "", html)
                except Exception:
                    txt = ""
            html = None
            return txt or ""
        pages = await asyncio.gather(*[_fetch_careers(p) for p in _CAREERS_PATHS])
        body  = next((p for p in pages if p and len(p) > 200), "")
        if body:
            lines.append(f"[careers page]\n{body[:1500]}")

    text = "\n".join(lines) if lines else "[No hiring data found]"
    return text, newest


# =============================================================================
# L5: Wikipedia background summary (free REST API)
# =============================================================================
async def _layer5_wikipedia(client, company_name: str) -> str:
    try:
        slug = urllib.parse.quote(company_name.replace(" ", "_"))
        url  = f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
        raw  = await _fetch_html(client, url, cap=_WIKI_CAP)
        if not raw:
            return ""
        import json as _json
        data = _json.loads(raw)
        if data.get("type") in ("disambiguation", "no-extract"):
            return ""
        return (data.get("extract") or "")[:WIKI_CHAR_BUDGET]
    except Exception:
        return ""


# =============================================================================
# L6: Targeted pain-signal search (sender-derived hints, optional)
#
# Runs ONLY when the caller supplies search_hints from derive_search_hints().
# Hints come from the sender's capability_to_pain_map pain side + campaign prompt
# key phrases — fully domain-agnostic, no hardcoded vocabulary here.
# Two queries: one for general web signals, one scoped to job boards.
# =============================================================================
async def _layer6_pain_signals(
    company_name: str,
    tokens: list[str],
    search_hints: list[str],
) -> str:
    """Targeted DDGS search using sender-derived pain phrases as query modifiers.

    Queries are built entirely from search_hints — no fixed terms added here.
    The caller (derive_search_hints) owns the vocabulary; this layer just
    executes the search and returns raw snippets for the LLM to interpret.
    """
    if not search_hints:
        return "[L6 skipped — no search hints supplied]"

    # Cap to 5 hints to keep OR clause length manageable across all senders.
    top = search_hints[:5]
    hints_or = " OR ".join(f'"{h}"' for h in top)

    queries = [
        # General operational signal search.
        f'"{company_name}" ({hints_or}) {_YEAR_TERMS}',
        # Job-board scoped: looks for job postings mentioning these pain terms.
        f'"{company_name}" ({hints_or}) (jobs OR careers OR hiring) {_YEAR_TERMS}',
    ]

    def _search():
        from ddgs import DDGS
        hits, seen = [], set()
        with DDGS() as d:
            for q in queries:
                try:
                    for h in d.text(q, timelimit="y", max_results=4):
                        u = h.get("href", "")
                        if u and u not in seen:
                            seen.add(u)
                            hits.append(h)
                except Exception:
                    continue
        return hits

    loop = asyncio.get_event_loop()
    async with _sem("ddgs", DDGS_CONCURRENCY):
        try:
            hits = await asyncio.wait_for(
                loop.run_in_executor(None, _search), timeout=20)
        except Exception as exc:
            return f"[L6 failed: {exc}]"

    kept = []
    for h in hits:
        title = (h.get("title") or "").strip()
        body  = (h.get("body") or "").strip()
        if _mentions_company(f"{title} {body}", company_name, tokens):
            kept.append(f"- {title}\n  {body[:200]}")

    return "\n\n".join(kept) if kept else "[No targeted pain-signal results]"


# =============================================================================
# LLM extraction — fuses all layers into a CompanyEnrichment record
# =============================================================================
_ENRICH_PROMPT = """\
You are a B2B data-enrichment analyst. COLLECT signal-level company intelligence for a
downstream ICP system. Do NOT score or judge fit.

SCOPE: Firmographics (HQ, locations, headcount, revenue, founded year, base industry) are
already known elsewhere -- DO NOT report them.

RULES:

1. RECENCY GATE — applies ONLY to growth_signals and recent_events:
   a) INCLUDE: events you can attribute to a SPECIFIC SOURCE that is clearly dated {signal_cutoff_year}
      or later (e.g. a press release headline with a date, a Google News item with a pubDate).
      A news headline or article URL with a visible date is enough. An event mentioned on the
      company website with NO date attached is NOT enough on its own.
   b) OMIT (do NOT include in growth_signals/recent_events): anything where the date is absent,
      uncertain, or could be older than 12 months. When in doubt, OMIT — do not guess.
   c) MOVE to company_history: events you CAN date to before {signal_cutoff_year}.
   CRITICAL: growth_signals and recent_events must be FACTUAL EVENTS, not interpretations.
   Never write "indicates a need for…", "suggests pressure to…", or any analysis of what an event
   means — that reasoning belongs in pain_points. Write only the event itself (what happened, when).

2. ENTITY FILTER: growth_signals and recent_events MUST be about {company_name} ITSELF, not
   partners, industry trends, or other organisations.

3. INFERENCE FIELD DISCIPLINE — where inferences belong:
   pain_points    = your INFERENCES about operational challenges (infer from hiring, growth, etc.)
   key_initiatives = programs the company has NAMED in its own materials
   growth_signals  = DATED FACTS only (no "indicates…", no "suggests…")
   recent_events   = DATED FACTS only (same rule)

4. TECHNOLOGY vs CERTIFICATION:
   technologies   = software, SaaS, cloud, CRM/ERP/analytics tools USED to run the business.
   certifications = quality/compliance standards EARNED (ISO 9001, SOC 2, CE marking, etc.).

5. SOURCE QUALITY:
   - For numeric facts (revenue, employee count, headcount, capacity): ONLY include if sourced
     from the company's own website or an official press release / news article. REJECT estimates
     from third-party aggregators (ZoomInfo, Owler, Craft.co, Dun & Bradstreet, Growjo, LeadIQ,
     Tracxn, etc.) — they are modelled estimates, not verified facts.
   - Cross-check source URLs against the company domain ({domain}). If a search snippet clearly
     refers to a different company that shares a word in the name, IGNORE it.

6. OTHER RULES:
   - hiring_signals: use the LIVE HIRING section for actual job postings.
   - pain_points: Write 2–4 inferences about operational or strategic pressures this specific
     company faces, grounded in what you know about their products, customers, certifications,
     recent activity, and industry position from the context below. This field is deliberately
     analytical — it is WHERE inferences belong (not in growth_signals or recent_events).
     RULES:
       a) SYNTHESISE, don't restate. If a fact is in growth_signals/recent_events, do NOT copy
          it as "The [fact] indicates a need for X." Instead, draw a DIFFERENT operational
          implication. Example: if growth_signals has "signed Deutsche Aircraft partnership",
          a valid pain_point is "Simultaneous OEM commitments across ILA, Deutsche Aircraft,
          and Wichita centre create competing production schedule demands" — not
          "The partnership indicates a need for increased capacity."
       b) Ground each bullet in something company-specific from the context (their product
          complexity, their customer base, their certifications, their industry, their scale).
          Generic sector statements that apply to every company in the sector are not allowed.
       c) Phrase as a concrete pressure, not a hedge:
          GOOD: "Bespoke composite manufacturing for defence/aero customers requires
                 concurrent ITAR and AS9100 compliance overhead across every product line"
          BAD:  "may face supply chain challenges due to global partners"
       d) Return [] only if the context is so thin (e.g. a placeholder landing page with no
          product, customer, or operational detail) that no grounded inference is possible.
   - awards_recognition: awards and public recognition only (not certifications).
   - key_initiatives: strategic programs named in the sources; no invented phrases.
   - Return [] for fact lists (growth_signals, recent_events, hiring_signals) when evidence
     is absent. pain_points should always have 2–4 bullets unless the context is truly empty.
   - Mine overview, products_services, and target_customers thoroughly from the website.

COMPANY: {company_name}  |  DOMAIN: {domain}

=== WEBSITE CONTENT (overview, products, customers) ===
{site_content}

=== WIKIPEDIA / BACKGROUND (history, M&A, public status) ===
{wiki_content}

=== COMPANY OWNED NEWS (RSS feed + press/blog/customer pages, dated) ===
{owned_content}

=== GOOGLE NEWS (3rd-party, recent first) ===
{news_content}

=== WEB SIGNALS (DDGS past year: growth + hiring + awards + challenges) ===
{ddgs_content}

=== LIVE HIRING (careers pages + ATS job boards) ===
{hiring_content}

=== DETECTED TECH STACK ===
{tech_stack}

=== TARGETED PAIN SIGNALS (campaign-specific search — may contain job postings or
    press releases mentioning the sender's pain vocabulary; empty when not supplied) ===
{pain_signals_content}

Produce the CompanyEnrichment record now.
"""


async def _llm_extract(
    company_name: str, domain: str,
    site_content: str, wiki_content: str, owned_content: str,
    news_content: str, ddgs_content: str, hiring_content: str,
    tech_stack: list[str],
    pain_signals_content: str = "",
) -> Optional[CompanyEnrichment]:
    from app.core.llm import get_chat_llm
    from app.core.logging_config import agent_label_var, company_domain_var
    llm   = get_chat_llm("enrichment", timeout=90)
    chain = llm.with_structured_output(CompanyEnrichment)
    prompt = _ENRICH_PROMPT.format(
        company_name=company_name, domain=domain,
        signal_cutoff_year=_SIGNAL_CUTOFF_YEAR,
        site_content=_prefilter_enrichment(site_content, SITE_CHAR_BUDGET),
        wiki_content=(wiki_content or "[No Wikipedia entry]")[:WIKI_CHAR_BUDGET],
        owned_content=(owned_content or "[None found]")[:PRESS_CHAR_BUDGET],
        news_content=news_content[:NEWS_CHAR_BUDGET],
        ddgs_content=ddgs_content[:DDGS_CHAR_BUDGET],
        hiring_content=hiring_content[:HIRING_CHAR_BUDGET],
        tech_stack=", ".join(tech_stack) if tech_stack else "None detected",
        pain_signals_content=(pain_signals_content or "[Not supplied]")[:PAIN_SIGNALS_CHAR_BUDGET],
    )
    # Label this call so cost tracking knows which agent + company produced the spend.
    _dom_tok = company_domain_var.set(domain)
    _ag_tok  = agent_label_var.set("enrichment_v2")
    try:
        rec: CompanyEnrichment = await chain.ainvoke(prompt)
        return rec
    except Exception as e:
        logger.warning(f"[EnrichV2] LLM extraction failed for {company_name}: {e}")
        return None
    finally:
        agent_label_var.reset(_ag_tok)
        company_domain_var.reset(_dom_tok)


# =============================================================================
# Core enrichment function — takes the shared client, no session creation here
# =============================================================================
async def _enrich_inner(name: str, domain: str, client, search_hints: list[str]) -> dict:
    """Run all layers + LLM extraction for one company.

    `client` is a shared curl_cffi AsyncSession supplied by the caller.
    `search_hints` is a list of sender-derived pain phrases (from derive_search_hints).
    When non-empty, Layer 6 runs a targeted pain-signal DDGS search using those hints.
    This function NEVER creates its own session — the caller owns that.
    """
    tokens = _name_tokens(name)
    logger.info(f"[EnrichV2] Starting {name} ({domain})")

    # Run L1 first so we know if the site is unreachable before launching L3.
    # L3 fires extra fallback queries when the site can't be crawled.
    l1 = await _layer1_site_tech_rss(client, domain)
    if isinstance(l1, Exception):
        l1 = ("[L1 error]", "", [], [], None)

    site_text, press_text, techs, owned_news, owned_newest = l1
    site_unreachable = any(tag in site_text for tag in ("[CRAWL FAIL", "[L1 error]", "[CRAWL ERROR]"))

    # Run remaining layers in parallel, passing site_unreachable to L3.
    l2, l3, l4, l5, l6 = await asyncio.gather(
        _layer2_google_news(client, name, tokens),
        _layer3_ddgs(name, tokens, site_unreachable=site_unreachable),
        _layer4_hiring(client, domain, name),
        _layer5_wikipedia(client, name),
        _layer6_pain_signals(name, tokens, search_hints),
        return_exceptions=True,
    )

    # Coerce any layer exception into safe empties.
    if isinstance(l2, Exception): l2 = ("[L2 error]", None)
    if isinstance(l3, Exception): l3 = "[L3 error]"
    if isinstance(l4, Exception): l4 = ("[L4 error]", None)
    if isinstance(l5, Exception): l5 = ""
    if isinstance(l6, Exception): l6 = "[L6 error]"
    news_text, news_newest                                  = l2
    ddgs_text                                               = l3
    hiring_text, hire_newest                                = l4
    wiki_text                                               = l5
    pain_signals_text                                       = l6

    owned_block = (("\n".join(owned_news) + "\n\n") if owned_news else "") + press_text

    # LLM extraction (async, so it doesn't block the event loop).
    rec = await _llm_extract(
        name, domain, site_text, wiki_text, owned_block,
        news_text, ddgs_text, hiring_text, techs,
        pain_signals_content=pain_signals_text,
    )

    # Degraded fallback: always emit a record even when LLM fails.
    if rec is None:
        snippet = re.sub(r"\s+", " ", site_text).strip()[:280]
        rec = CompanyEnrichment(
            growth_signals=[], recent_events=[], hiring_signals=[], pain_points=[],
            overview=snippet or "Unknown", products_services=[], target_customers="Unknown",
            notable_customers_partners=[], technologies=techs,
            certifications=[], awards_recognition=[], key_initiatives=[], company_history=[],
        )
        status = "degraded"
    else:
        status = "ok"

    # Merge fingerprinted techs (deterministic) with LLM-extracted techs.
    rec.technologies = list(dict.fromkeys([*techs, *rec.technologies]))
    # Move any pre-cutoff events to company_history (stale-signal safety net).
    rec = _filter_stale_signals(rec)

    result = rec.model_dump()
    result["_schema"] = "v2"
    result["_status"] = status
    result["_techs"]  = techs

    n_growth = len(rec.growth_signals)
    n_events = len(rec.recent_events)
    l6_tag   = f" hints={len(search_hints)}" if search_hints else ""
    logger.info(f"[EnrichV2] Done {name}: status={status} growth={n_growth} events={n_events} hiring={len(rec.hiring_signals)}{l6_tag}")
    return result


# =============================================================================
# Public API
# =============================================================================
async def enrich_company(
    name: str,
    domain: str,
    *,
    client=None,
    search_hints: list[str] | None = None,
) -> dict | None:
    """Enrich one company using the multi-layer v2 pipeline.

    Args:
        name:         Company display name.
        domain:       Company root domain (e.g. "acme.com").
        client:       A shared curl_cffi AsyncSession.  MUST be supplied in
                      production — a single shared session across concurrent
                      companies keeps peak RAM low. Only omit in unit tests.
        search_hints: Optional list of sender-derived pain phrases produced by
                      derive_search_hints(user_intel, campaign_prompt). When
                      supplied, Layer 6 runs a targeted DDGS search using these
                      phrases so the enrichment record surfaces pain-signal
                      evidence relevant to THIS sender. When omitted, L6 is a
                      no-op and only the 5 generic layers run.

    Returns:
        dict with CompanyEnrichment fields plus:
            _schema:  "v2"
            _status:  "ok" | "degraded"
            _techs:   list[str] of HTML-fingerprinted tech names

        Returns None only on hard failure; the degraded path covers LLM failure.
    """
    if not name or not domain:
        return None
    hints = search_hints or []
    try:
        if client is not None:
            return await asyncio.wait_for(
                _enrich_inner(name, domain, client, hints), timeout=HARD_TIMEOUT_SEC)
        else:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession(impersonate="chrome", max_clients=4) as _c:
                return await asyncio.wait_for(
                    _enrich_inner(name, domain, _c, hints), timeout=HARD_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning(f"[EnrichV2] Timeout ({HARD_TIMEOUT_SEC}s) for {name} ({domain})")
        return None
    except Exception as e:
        logger.error(f"[EnrichV2] Fatal error for {name} ({domain}): {e}")
        return None
