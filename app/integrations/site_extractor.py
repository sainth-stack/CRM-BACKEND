"""site_extractor.py — company-site extractor (curl_cffi + trafilatura).

Ported from the root reference tool (`company_site_extractor.py`, fast mode) and
hardened for the backend: SSRF-validated entry, bounded memory/latency, no paid
search API. Used by the Stage-2 brand-intelligence agent.

Latency/RAM properties:
  * ONE homepage fetch (robust: https/http + www variants, alt-impersonation on
    403, TLS-bypass on cert error, /path probe on 404) + ONE parallel wave of
    high-value pages — at most 2 network round-trips total.
  * HTML is capped at 200 KB before trafilatura (lxml parse of huge DOMs holds the
    GIL); main content lives near the top, so quality is preserved.
  * Each page's text is capped at `per_page_chars`; at most `max_pages` pages.
  * trafilatura parsing is offloaded to threads so the event loop never blocks.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import trafilatura
from curl_cffi.requests import AsyncSession  # browser-TLS fingerprint -> defeats Cloudflare 403

from app.core.logging_config import logger
from app.core.security import validate_url_for_ssrf

# Junk we never crawl.
SKIP_PATTERNS = (
    "privacy", "cookie", "terms", "legal", "login", "signin", "sign-in", "signup",
    "sign-up", "/tag/", "/category/", "wp-", ".pdf", ".jpg", ".jpeg", ".png", ".svg",
    ".gif", ".zip", ".xml", ".css", ".js", "mailto:", "tel:", "javascript:",
    "twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com",
    "youtube.com", "t.me", "wa.me",
)
# Hint keywords used to rank candidate links.
VALUE_KEYWORDS = (
    "about", "company", "who-we-are", "story", "mission", "product", "solution",
    "platform", "service", "industr", "sector", "market", "use-case", "capab",
    "customer", "client", "case-stud", "technology", "how-it-works", "feature",
    "pricing", "team", "leadership", "overview", "what-we-do",
)
# Conventional high-value B2B paths probed speculatively in the same wave.
COMMON_PATHS = (
    "about", "about-us", "company", "products", "solutions", "services",
    "industries", "what-we-do", "platform", "customers",
)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# Alternate browser fingerprint retried ONCE on a 403 (curl_cffi built-in). Kept to
# a single retry so a hard anti-bot wall costs ~one extra request, not several.
_ALT_IMPERSONATE = ("safari17_0",)


@dataclass
class PageResult:
    url: str
    path: str
    text: str = ""


@dataclass
class SiteExtract:
    domain: str
    final_url: str = ""
    ok: bool = False
    title: str = ""
    description: str = ""
    pages: list[PageResult] = field(default_factory=list)   # homepage first
    nav_paths: list[str] = field(default_factory=list)      # discovered high-value link paths
    word_count: int = 0
    elapsed_sec: float = 0.0
    error: str = ""

    @property
    def homepage_text(self) -> str:
        return self.pages[0].text if self.pages else ""

    @property
    def combined_text(self) -> str:
        """All extracted evidence as one block (meta + per-page), for LLM judging."""
        blocks = ([f"[META DESCRIPTION] {self.description}"] if self.description else []) + \
                 [f"\n[PAGE {p.path}]\n{p.text}" for p in self.pages]
        return "\n".join(blocks).strip()

    @property
    def subpages(self) -> list[PageResult]:
        return self.pages[1:] if len(self.pages) > 1 else []


# --------------------------------------------------------------------------- #
# Fetch + extract helpers (ported from the reference tool)                      #
# --------------------------------------------------------------------------- #
def _norm(domain: str) -> str:
    d = domain.strip()
    return d if d.startswith(("http://", "https://")) else "https://" + d


def _same_site(base_host: str, url: str) -> bool:
    try:
        h = (urlparse(url).hostname or "").replace("www.", "")
    except Exception:
        return False
    base = base_host.replace("www.", "")
    return h == base or h.endswith("." + base)


def _jsonld_text(data, _depth: int = 0) -> str:
    if _depth > 6:
        return ""
    keys = ("name", "description", "slogan", "about", "industry", "knowsAbout",
            "alternateName", "disambiguatingDescription", "headline", "articleBody")
    out = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k in keys and isinstance(v, str):
                out.append(v)
            elif isinstance(v, (dict, list)):
                out.append(_jsonld_text(v, _depth + 1))
    elif isinstance(data, list):
        for it in data:
            out.append(_jsonld_text(it, _depth + 1))
    return " ".join(x for x in out if x)


def _fallback_text(html: str) -> str:
    """Recovery when trafilatura finds no main content: title/meta/og/JSON-LD + crude body."""
    parts: list[str] = []
    for pat in (r"<title[^>]*>(.*?)</title>",
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+name=["\']keywords["\'][^>]+content=["\']([^"\']+)'):
        for mm in re.findall(pat, html, flags=re.IGNORECASE | re.DOTALL):
            parts.append(re.sub(r"\s+", " ", mm).strip())
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                            html, flags=re.IGNORECASE | re.DOTALL):
        try:
            parts.append(_jsonld_text(json.loads(block.strip())))
        except Exception:
            pass
    body = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html[:300_000])
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body.split()) >= 40:
        parts.append(body[:4000])
    return " ".join(p for p in parts if p).strip()


def _clean(html: str) -> str:
    if not html:
        return ""
    main = (trafilatura.extract(html[:200_000], include_comments=False, include_tables=True,
                                favor_recall=True, no_fallback=False) or "").strip()
    text = main if len(main) >= 80 else (lambda fb: fb if len(fb) > len(main) else main)(_fallback_text(html))
    # Strip NUL bytes at the source: Postgres TEXT cannot store 0x00, and this text
    # ends up persisted as icp_research_context. Cheap no-op when absent.
    return text.replace("\x00", "") if "\x00" in text else text


def _harvest_links(base_url: str, html: str, base_host: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for href, inner in re.findall(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
                                  html, flags=re.IGNORECASE | re.DOTALL):
        if any(s in href.lower() for s in SKIP_PATTERNS):
            continue
        full = urljoin(base_url, href).split("?")[0].rstrip("/")
        if not full.startswith("http") or not _same_site(base_host, full):
            continue
        anchor = re.sub(r"<[^>]+>", " ", inner)
        anchor = re.sub(r"\s+", " ", anchor).strip()[:80]
        if full not in out or (anchor and not out[full]):
            out[full] = anchor
    return out


def _classify(err: str, status: int) -> str:
    e = (err or "").lower()
    if status == 404:
        return "404_not_found"
    if status == 403:
        return "403_blocked (anti-bot / Cloudflare)"
    if status in (401, 451):
        return f"{status}_blocked"
    if status and status >= 500:
        return f"{status}_server_error"
    if "could not resolve" in e or "name or service" in e or "curl: (6)" in e or "nxdomain" in e:
        return "dns_unresolved"
    if "ssl" in e or "certificate" in e or "curl: (60)" in e or "curl: (35)" in e:
        return "ssl_error"
    if "timeout" in e or "timed out" in e or "curl: (28)" in e:
        return "timeout"
    if "connect" in e or "refused" in e or "curl: (7)" in e:
        return "connection_refused"
    if "empty" in e or "no content" in e:
        return "empty_or_js_only"
    return err or "unknown"


async def _afetch(client: "AsyncSession", url: str, *, connect: float = 3.0,
                  read: float = 7.0, impersonate: str | None = None,
                  verify: bool = True) -> tuple[str, int, str, str]:
    """Return (final_url, status, html, err). Never raises."""
    try:
        kw = {"allow_redirects": True, "timeout": (connect, read)}
        if impersonate:
            kw["impersonate"] = impersonate
        if not verify:
            kw["verify"] = False
        r = await client.get(url, **kw)
        html = r.text if r.status_code < 400 else ""
        err = "" if html else f"http_{r.status_code}"
        return str(r.url), r.status_code, html, err
    except Exception as e:
        return url, 0, "", f"{type(e).__name__}: {str(e)[:100]}"


async def _robust_homepage(client: "AsyncSession", base: str, *, read: float = 8.0,
                           connect: float = 3.0) -> tuple[str, int, str, str]:
    """Resolve a homepage in one bounded pass. https/http + www variants; the expensive
    sub-recoveries (alt-impersonation on 403, TLS-ignore on cert error, /path probe on
    404) each run AT MOST ONCE total so dead/blocked sites can't burn 30-60s."""
    host = base.split("://", 1)[-1].rstrip("/")
    bare = host.replace("www.", "")
    variants, seen = [], set()
    for v in (f"https://{bare}", f"https://www.{bare}", f"http://{bare}"):
        if v not in seen:
            seen.add(v)
            variants.append(v)

    last = (base, 0, "", "unknown")
    did_alt = did_ssl = did_404 = False
    for v in variants:
        furl, st, html, err = await _afetch(client, v, connect=connect, read=read)
        if html:
            return furl, st, html, ""
        if st or last[1] == 0:
            last = (furl, st, html, err)
        if st == 403 and not did_alt:
            did_alt = True
            for imp in _ALT_IMPERSONATE:
                f3, s3, h3, _e = await _afetch(client, v, connect=connect, read=read, impersonate=imp)
                if h3:
                    return f3, s3, h3, ""
        el = (err or "").lower()
        if (not did_ssl) and any(s in el for s in ("ssl", "certificate", "curl: (60)", "curl: (35)", "curl: (58)")):
            did_ssl = True
            f4, s4, h4, _e = await _afetch(client, v, connect=connect, read=read, verify=False)
            if h4:
                return f4, s4, h4, ""
        if st == 404 and not did_404:
            did_404 = True
            for path in ("en", "home", "index.html"):
                f2, s2, h2, _e = await _afetch(client, f"{v}/{path}", connect=connect, read=read)
                if h2:
                    return f2, s2, h2, ""
    return last[0], last[1], last[2], _classify(last[3], last[1])


def _heuristic_pick(candidates: dict[str, str], budget: int) -> list[str]:
    scored = []
    for u, a in candidates.items():
        path = urlparse(u).path.lower()
        score = sum(1 for k in VALUE_KEYWORDS if k in path or k in (a or "").lower())
        if score:
            scored.append((score * 10 - path.count("/"), u))
    scored.sort(reverse=True)
    return [u for _, u in scored[:budget]]


@contextlib.asynccontextmanager
async def _session(client):
    if client is not None:
        yield client
    else:
        async with AsyncSession(impersonate="chrome", headers=HEADERS, max_clients=10) as c:
            yield c


# --------------------------------------------------------------------------- #
# Public entry                                                                 #
# --------------------------------------------------------------------------- #
async def extract_site(domain: str, *, client=None, max_pages: int = 5,
                       per_page_chars: int = 7000, timeout: float = 6.0) -> SiteExtract:
    """Homepage + ONE parallel wave of high-value pages. SSRF-validated entry.

    Returns a SiteExtract (homepage page first). Never raises — failures are
    reported via `.ok=False` and `.error`.
    """
    t0 = time.time()
    base = _norm(domain).rstrip("/")
    base_host = (urlparse(base).hostname or domain).replace("www.", "")
    out = SiteExtract(domain=domain)

    # SSRF boundary: reject internal/loopback/metadata targets before any fetch.
    try:
        validate_url_for_ssrf(base)
    except Exception as e:
        out.error = f"ssrf_blocked: {str(e)[:120]}"
        out.elapsed_sec = round(time.time() - t0, 2)
        return out

    pages: dict[str, str] = {}
    try:
        async with _session(client) as client:
            furl, st, home_html, reason = await _robust_homepage(client, base, read=max(timeout, 8.0))
            out.final_url = furl or base
            if not home_html:
                out.error = reason or f"homepage unreachable (HTTP {st})"
                out.elapsed_sec = round(time.time() - t0, 2)
                return out

            home_txt = await asyncio.to_thread(_clean, home_html)
            if len(home_txt) >= 80:
                pages[out.final_url.rstrip("/")] = home_txt
            meta = await asyncio.to_thread(trafilatura.extract_metadata, home_html)
            if meta:
                out.title = (meta.title or "")[:300]
                out.description = (meta.description or "")[:600]

            p = urlparse(out.final_url)
            rbase = f"{p.scheme}://{p.netloc}"

            discovered = _harvest_links(out.final_url, home_html, base_host)
            picked = _heuristic_pick(discovered, max_pages)
            out.nav_paths = sorted({urlparse(u).path for u in picked if urlparse(u).path not in ("", "/")})

            wave, seen = [], {out.final_url.rstrip("/")}
            for u in picked + [f"{rbase}/{c}" for c in COMMON_PATHS]:
                uu = u.rstrip("/")
                if uu not in seen:
                    seen.add(uu)
                    wave.append(u)
            wave = wave[:max_pages]

            if wave:
                results = await asyncio.gather(*[_afetch(client, u) for u in wave])
                cleaned = await asyncio.gather(*[asyncio.to_thread(_clean, h) for _, _, h, _e in results])
                for (furl2, _st2, html2, _e2), txt in zip(results, cleaned):
                    if html2 and len(txt) >= 120:
                        pages.setdefault(furl2.rstrip("/"), txt)
    except Exception as e:
        logger.warning(f"[SITE EXTRACTOR] {domain}: {type(e).__name__}: {str(e)[:120]}")
        if not pages:
            out.error = f"{type(e).__name__}: {str(e)[:120]}"
            out.elapsed_sec = round(time.time() - t0, 2)
            return out

    # Assemble: homepage first, then largest pages, capped to max_pages.
    home_key = out.final_url.rstrip("/")
    items = sorted(pages.items(), key=lambda kv: (kv[0] != home_key, -len(kv[1])))
    for u, txt in items[:max_pages]:
        out.pages.append(PageResult(url=u, path=urlparse(u).path or "/", text=txt[:per_page_chars]))

    out.word_count = sum(len(p.text.split()) for p in out.pages)
    out.ok = out.word_count > 30
    if not out.ok and not out.error:
        out.error = "empty_or_js_only (page loaded but no server-side text)"
    out.elapsed_sec = round(time.time() - t0, 2)
    return out
