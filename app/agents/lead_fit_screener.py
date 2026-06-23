"""LeadFitScreener — Stage 3A pre-filter gate.

Eliminates clearly unfit companies BEFORE the expensive ICP pipeline
(web crawl + enrichment LLM + reasoning LLM). Operates exclusively on
CSV firmographic data already in unique_cos — zero additional scraping.

Three checks, run in order:
  1. Location  — deterministic, continent/region-aware via pycountry-convert.
                 Handles countries, continents (Europe, Asia), and business
                 regions (APAC, LATAM, MENA, DACH, Nordics, Benelux, GCC...).
  2. Size      — deterministic, numeric range overlap (reuses ICP logic).
  3. Industry  — single gpt-4o-mini call using industry label + Seamless
                 description + SIC/NAICS context + sender target profile.
                 ALWAYS runs regardless of SIC/NAICS resolution — the
                 description is the primary evidence for actual business fit.

Verdict:
  FAIL → any single check is a hard FAIL → caller marks PRE_SCREENED_REJECT.
  PASS → all checks are PASS or UNKNOWN  → caller proceeds to ICP.
  UNKNOWN alone is never a blocker (conservative gate — don't kill real prospects).
"""
from __future__ import annotations

import re
import logging
from typing import Literal

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

from app.core.llm import get_chat_llm

logger = logging.getLogger("LeadFitScreener")


# --------------------------------------------------------------------------- #
# Output schemas                                                               #
# --------------------------------------------------------------------------- #

class CheckResult(BaseModel):
    verdict: Literal["pass", "fail", "unknown"]
    evidence: str


class IndustryScreenVerdict(BaseModel):
    verdict: Literal["pass", "fail", "unknown"] = Field(
        description=(
            "'pass'  = the company's actual business clearly falls under the target industry. "
            "'fail'  = the company is CLEARLY in a DIFFERENT supercategory than EVERY target "
            "industry — reserve for unambiguous mismatches ONLY (e.g. a law firm against "
            "Manufacturing, a barbershop against Technology). "
            "'unknown' = description too thin, vague, or genuinely ambiguous to decide."
        )
    )
    evidence: str = Field(
        description=(
            "One sentence citing SPECIFIC text from the description that drove the verdict. "
            "If unknown, state what was missing."
        )
    )
    actual_business: str = Field(
        description="One sentence: what this company actually does, read from the description."
    )


class ScreenerOutput(BaseModel):
    company_name: str
    location_check: CheckResult
    size_check: CheckResult
    industry_check: CheckResult
    final_verdict: Literal["PASS", "FAIL"]
    failed_checks: list[str]
    summary: str
    token_usage: dict = {}   # input_tokens, output_tokens, cached_tokens


# --------------------------------------------------------------------------- #
# Geography helpers — continent / region expansion                             #
# --------------------------------------------------------------------------- #

# Business sub-regions → ISO alpha-2 sets (deterministic, no pycountry needed)
_SPECIAL_REGION_ALPHA2: dict[str, set[str]] = {
    "mena": {
        "DZ","BH","DJ","EG","IQ","JO","KW","LB","LY","MR","MA","OM","PS",
        "QA","SA","SO","SD","SY","TN","AE","YE","IR","IL","TR",
    },
    "middle east and north africa": {
        "DZ","BH","DJ","EG","IQ","JO","KW","LB","LY","MR","MA","OM","PS",
        "QA","SA","SO","SD","SY","TN","AE","YE","IR","IL","TR",
    },
    "middle east": {"AE","SA","QA","KW","BH","OM","YE","JO","IQ","SY","LB","IR","IL","PS","TR"},
    "dach": {"DE","AT","CH"},
    "nordics": {"DK","FI","NO","SE","IS"},
    "scandinavia": {"DK","FI","NO","SE","IS"},
    "benelux": {"BE","NL","LU"},
    "cee": {
        "PL","CZ","SK","HU","RO","BG","HR","SI","RS","AL","MK","BA","ME","XK",
        "LV","LT","EE","UA","BY","MD",
    },
    "central and eastern europe": {
        "PL","CZ","SK","HU","RO","BG","HR","SI","RS","AL","MK","BA","ME","XK",
        "LV","LT","EE","UA","BY","MD",
    },
    "gcc": {"SA","AE","QA","KW","BH","OM"},
    "gulf cooperation council": {"SA","AE","QA","KW","BH","OM"},
    "anz": {"AU","NZ"},
    "sea": {"SG","MY","TH","VN","PH","ID","MM","KH","LA","BN","TL"},
    "southeast asia": {"SG","MY","TH","VN","PH","ID","MM","KH","LA","BN","TL"},
    "south asia": {"IN","PK","BD","LK","NP","BT","MV","AF"},
    "east asia": {"CN","JP","KR","TW","HK","MO"},
    "latam": {
        "MX","GT","BZ","HN","SV","NI","CR","PA","CU","JM","HT","DO","PR",
        "CO","VE","GY","SR","EC","PE","BO","BR","PY","UY","AR","CL","TT",
    },
    "latin america": {
        "MX","GT","BZ","HN","SV","NI","CR","PA","CU","JM","HT","DO","PR",
        "CO","VE","GY","SR","EC","PE","BO","BR","PY","UY","AR","CL","TT",
    },
    "apac": {
        "CN","JP","KR","TW","HK","MO","SG","MY","TH","VN","PH","ID","MM","KH","LA","BN",
        "IN","PK","BD","LK","NP","BT","MV","AF","AU","NZ","FJ","PG","WS","TO","VU","TL",
    },
    "asia pacific": {
        "CN","JP","KR","TW","HK","MO","SG","MY","TH","VN","PH","ID","MM","KH","LA","BN",
        "IN","PK","BD","LK","NP","BT","MV","AF","AU","NZ","FJ","PG","WS","TO","VU","TL",
    },
    "asia-pacific": {
        "CN","JP","KR","TW","HK","MO","SG","MY","TH","VN","PH","ID","MM","KH","LA","BN",
        "IN","PK","BD","LK","NP","BT","MV","AF","AU","NZ","FJ","PG","WS","TO","VU","TL",
    },
    "sub-saharan africa": {
        "NG","KE","ZA","GH","ET","TZ","UG","CM","CI","SN","ZM","ZW","MZ","AO","MG",
        "BF","ML","RW","TD","SO","MR","NE","SS","CD","CG","GA","GN","SL","LR","TG",
    },
    "north africa": {"EG","LY","TN","DZ","MA","SD"},
    "west africa": {"NG","GH","CI","SN","ML","BF","GN","SL","LR","TG","BJ","NE","MR","GW","CV","GM"},
    "east africa": {"KE","ET","TZ","UG","RW","SO","DJ","ER","MG","MZ","ZM","ZW","MW","SC"},
    "southern africa": {"ZA","ZM","ZW","MZ","BW","NA","LS","SZ","AO","MG","MU","MW"},
}

# Standard continent name → ISO continent code (for pycountry-convert)
_CONTINENT_NAME_TO_CODE: dict[str, str] = {
    "africa": "AF",
    "north america": "NA",
    "south america": "SA",
    "europe": "EU",
    "asia": "AS",
    "oceania": "OC",
    "australia": "OC",
    "antarctica": "AN",
}

# Common country name/alias → ISO alpha-2 (handles spellings pycountry may miss)
_COUNTRY_ALIAS_TO_ALPHA2: dict[str, str] = {
    "usa": "US", "us": "US", "america": "US", "united states": "US",
    "united states of america": "US",
    "uk": "GB", "britain": "GB", "great britain": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB", "gb": "GB",
    "uae": "AE", "united arab emirates": "AE",
    "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "north korea": "KP",
    "czechia": "CZ", "czech republic": "CZ",
    "russia": "RU", "russian federation": "RU",
    "iran": "IR",
    "syria": "SY",
    "taiwan": "TW",
    "vietnam": "VN", "viet nam": "VN",
    "venezuela": "VE",
    "bolivia": "BO",
    "moldova": "MD",
    "tanzania": "TZ",
    "new zealand": "NZ",
    "saudi arabia": "SA",
    "south africa": "ZA",
    "hong kong": "HK",
    "macau": "MO", "macao": "MO",
    "ivory coast": "CI", "cote d'ivoire": "CI",
    "myanmar": "MM", "burma": "MM",
    "palestine": "PS",
    "laos": "LA",
    "timor-leste": "TL", "east timor": "TL",
}

_BLANK_VALUES = {"", "n/a", "na", "none", "null", "unknown", "any", "-"}


def _is_blank(v: str) -> bool:
    return (v or "").strip().lower() in _BLANK_VALUES


def _country_to_alpha2(text: str) -> str | None:
    """Resolve a country name / alias string to ISO alpha-2. Returns None if unresolvable."""
    if not text or _is_blank(text):
        return None
    t = text.strip().lower()

    # 1. Fast alias table
    if t in _COUNTRY_ALIAS_TO_ALPHA2:
        return _COUNTRY_ALIAS_TO_ALPHA2[t]

    # 2. pycountry exact + fuzzy
    try:
        import pycountry
        c = pycountry.countries.get(name=text.strip())
        if c:
            return c.alpha_2
        results = pycountry.countries.search_fuzzy(text.strip())
        if results:
            return results[0].alpha_2
    except Exception:
        pass

    return None


def _continent_alpha2_set(continent_code: str) -> set[str]:
    """All alpha-2 codes belonging to a continent (AF/NA/SA/EU/AS/OC/AN)."""
    result: set[str] = set()
    try:
        import pycountry
        import pycountry_convert as pc
        for country in pycountry.countries:
            try:
                if pc.country_alpha2_to_continent_code(country.alpha_2) == continent_code:
                    result.add(country.alpha_2)
            except Exception:
                continue
    except ImportError:
        logger.warning("[LeadFitScreener] pycountry-convert not installed — continent lookup skipped.")
    return result


def _expand_target_to_alpha2_set(target_loc: str) -> set[str] | None:
    """
    If target_loc names a continent or known business region, return the set of
    alpha-2 codes it covers. Returns None when the target is a specific country
    or city/state (caller handles those via direct country matching).
    """
    t = target_loc.strip().lower()

    # Check longest-match first so "southeast asia" beats "asia"
    for region_key in sorted(_SPECIAL_REGION_ALPHA2, key=len, reverse=True):
        if region_key in t:
            return _SPECIAL_REGION_ALPHA2[region_key]

    # Standard continent names
    for cont_name, cont_code in sorted(_CONTINENT_NAME_TO_CODE.items(), key=lambda x: len(x[0]), reverse=True):
        if cont_name in t:
            if cont_name == "americas":
                return _continent_alpha2_set("NA") | _continent_alpha2_set("SA")
            return _continent_alpha2_set(cont_code)

    return None  # not a region — treat as country / city / state


# --------------------------------------------------------------------------- #
# Check 1: Location                                                            #
# --------------------------------------------------------------------------- #

def match_location(csv_country: str, target_loc: str) -> tuple[str, str]:
    """
    Continent/region-aware deterministic location match.

    Returns (verdict, evidence):
      'pass'    — company country is within the target location / region.
      'fail'    — company country is a KNOWN country clearly outside the target.
      'unknown' — cannot determine (missing data, unresolvable names).

    UNKNOWN is always a pass-through — never blocks a company.
    """
    if _is_blank(target_loc):
        return "pass", "No location requirement specified."
    if _is_blank(csv_country):
        return "unknown", "Company country not stated in CSV."

    csv_a2 = _country_to_alpha2(csv_country)

    # Check if target is a region / continent (may contain multiple targets: "USA, Europe")
    for part in re.split(r"[,/;|]|\bor\b", target_loc, flags=re.IGNORECASE):
        part = part.strip()
        if not part:
            continue

        target_set = _expand_target_to_alpha2_set(part)

        if target_set is not None:
            # Region / continent path
            if csv_a2 and csv_a2 in target_set:
                return "pass", f"'{csv_country}' is within the target region '{part}'."
            if csv_a2 and csv_a2 not in target_set:
                # Don't FAIL yet — another part of the target might still match
                continue
            # Can't resolve CSV country against region — unknown for this part
            continue
        else:
            # Specific country / city / state path
            # Substring match (e.g. target "India", csv "Bangalore, India")
            if part.lower() in (csv_country or "").lower():
                return "pass", f"CSV country '{csv_country}' matches target '{part}'."
            # Alpha-2 comparison
            tgt_a2 = _country_to_alpha2(part)
            if tgt_a2 and csv_a2 and tgt_a2 == csv_a2:
                return "pass", f"CSV country '{csv_country}' matches target '{part}'."

    # Exhausted all target parts without a PASS
    if csv_a2:
        # We identified the company's country — it didn't match anything → FAIL
        return "fail", (
            f"'{csv_country}' (resolved: {csv_a2}) is not within the "
            f"target location '{target_loc}'."
        )

    # Couldn't resolve the company's country → safe unknown
    return "unknown", (
        f"Could not resolve '{csv_country}' to a known country — "
        f"cannot confirm or deny match against '{target_loc}'."
    )


# --------------------------------------------------------------------------- #
# Check 2: Size (reuses ICP numeric range logic verbatim)                     #
# --------------------------------------------------------------------------- #

def _parse_ranges(text: str) -> list[tuple[int | float, int | float]]:
    """Parse employee-count ranges from a free-text string → [(low, high)]; high=inf for '5000+'."""
    if not text:
        return []
    t = text.lower().replace(",", "")
    ranges: list[tuple] = []
    for mt in re.finditer(r"(\d+)\s*\+", t):
        ranges.append((int(mt.group(1)), float("inf")))
    for mt in re.finditer(r"(\d+)\s*(?:-|to|–|—|−)\s*(\d+)", t):
        a, b = int(mt.group(1)), int(mt.group(2))
        ranges.append((min(a, b), max(a, b)))
    if not ranges:
        nums = [int(x) for x in re.findall(r"\d+", t)]
        if len(nums) == 1:
            ranges.append((nums[0], nums[0]))
        elif len(nums) >= 2:
            ranges.append((min(nums), max(nums)))
    return ranges


def match_size(csv_size: str, target_size: str) -> tuple[str, str]:
    """
    Numeric range overlap on the CSV employee-size field.

    Returns (verdict, evidence): 'pass' | 'fail' | 'unknown'.
    Non-numeric target or CSV → 'unknown' (safe pass-through).
    """
    if _is_blank(target_size):
        return "pass", "No employee-size requirement specified."
    if _is_blank(csv_size):
        return "unknown", "Company employee size not stated in CSV."

    comp = _parse_ranges(csv_size)
    tgt: list[tuple] = []
    for part in re.split(r"[,/;|]|\bor\b", target_size.lower()):
        tgt += _parse_ranges(part)

    if not comp:
        return "unknown", f"CSV size '{csv_size}' could not be parsed as a numeric range."
    if not tgt:
        return "unknown", f"Target size '{target_size}' could not be parsed as a numeric range."

    for cl, ch in comp:
        for tl, th in tgt:
            if cl <= th and tl <= ch:
                return "pass", (
                    f"CSV size '{csv_size}' overlaps with target band '{target_size}'."
                )

    return "fail", (
        f"CSV size '{csv_size}' is entirely outside the target band '{target_size}'."
    )


# --------------------------------------------------------------------------- #
# Check 3: Industry (LLM — always runs)                                       #
# --------------------------------------------------------------------------- #

# Static system prompt — contains ONLY rules/examples with zero variables.
# Sent as SystemMessage so OpenAI caches this prefix across every call in the run.
# Dynamic context (sender info, target industry, company data) goes in HumanMessage.
#
# DESIGN NOTE: This prompt is deliberately industry-agnostic. The target industry
# is injected at the end of build_system_message() — never hardcode industry-specific
# rules here, or the screener silently breaks for any non-manufacturing campaign.
_INDUSTRY_SCREEN_SYSTEM = """\
You are a B2B lead pre-screener doing INDUSTRY CLASSIFICATION only.

Answer this single question:
"Does this company's primary business belong to the TARGET INDUSTRY stated at the end
of this message?"

You are NOT judging sales fit, product relevance, or operational match.
Sender context is provided solely so you understand which industry sector is being
targeted. Do NOT use it to assess whether the sender's product would work for this
company.

VERDICTS:

"pass" — the company clearly belongs to the target industry:
  - Its PRIMARY business activity falls squarely within the target industry category.
  - The industry label, SIC/NAICS codes, or description language clearly confirm
    membership in that sector.
  - When the evidence tilts toward the target industry but is not definitive, prefer
    "pass" over "unknown" — a missed prospect is a worse outcome than a false lead.

"fail" — the company is clearly in a DIFFERENT industry supercategory:
  - Its core activities have NO meaningful overlap with the target industry.
  - Reserve FAIL for UNAMBIGUOUS mismatches only — not borderline judgement calls.
  - The company's own industry label and what it does must both point clearly away
    from the target sector.
  - IMPORTANT: A company that SERVES, DISTRIBUTES, or SELLS TO companies in the
    target industry is NOT automatically a FAIL — evaluate its own primary business,
    not its customer base. Only FAIL if the company's own core activity is clearly
    outside the target industry.

"unknown" — too ambiguous or too thin to decide:
  - Description is very short, generic, or uses only vague terms ("provider of
    solutions", "leading supplier of products").
  - The company could plausibly belong to the target industry OR to another sector.
  - Industry label and description give conflicting or unclear signals.
  - Genuinely borderline — the company straddles two industry categories.

CALIBRATION RULES:
  1. Wrong FAILs eliminate real prospects permanently. Wrong PASSes are caught by the
     deeper ICP qualification stage. Always err toward PASS or UNKNOWN over FAIL.
  2. UNKNOWN is always a pass-through — it never eliminates a company.
  3. SIC/NAICS codes provide supporting evidence but do not override a clear description.
     Descriptions and industry labels take precedence when they conflict with codes.
  4. If the industry label places the company clearly in the target sector AND the
     description does not explicitly contradict that — return PASS or UNKNOWN, not FAIL.
  5. Do not invent reasons to FAIL. If you are unsure, UNKNOWN is correct.\
"""


def _sic_naics_context(sic: str, naics: str) -> str:
    """Render a short human-readable SIC/NAICS context string for the prompt."""
    parts = []
    if sic and not _is_blank(sic):
        parts.append(f"SIC {sic}")
    if naics and not _is_blank(naics):
        parts.append(f"NAICS {naics}")
    return ", ".join(parts) if parts else "Not available"


# --------------------------------------------------------------------------- #
# Main agent class                                                             #
# --------------------------------------------------------------------------- #

class LeadFitScreener:
    """Stage 3A pre-filter gate — eliminates clearly unfit companies before ICP.

    Usage:
        screener = LeadFitScreener()
        result = await screener.screen(company_data, campaign_metadata, user_intel)
        if result.final_verdict == "FAIL":
            # skip ICP — mark PRE_SCREENED_REJECT
    """

    def __init__(self):
        # Single cheap model call (gpt-4o-mini) — no reasoning model needed here.
        self._llm = get_chat_llm("cheap", timeout=30)

    async def screen(
        self,
        company_data: dict,
        campaign_metadata: dict,
        user_intel: dict,
        callbacks: list | None = None,   # kept for API compat; not used internally
    ) -> ScreenerOutput:
        """
        Run all three checks and return a ScreenerOutput.

        company_data keys used:
          name, country, size (staff_count_range), industry,
          description, sic_code, naics_code

        campaign_metadata keys used:
          target_location, target_employee_count, target_industry, prompt

        user_intel keys used:
          target_customers, services (or offerings or core_offerings)
        """
        name = company_data.get("name") or "Unknown"
        csv_country = company_data.get("country") or company_data.get("location") or ""
        csv_size = company_data.get("size") or company_data.get("staff_count") or ""
        csv_industry = company_data.get("industry") or ""
        csv_desc = company_data.get("description") or ""
        csv_sic = company_data.get("sic_code") or ""
        csv_naics = company_data.get("naics_code") or ""

        target_loc = (campaign_metadata.get("target_location") or "").strip()
        target_size = (campaign_metadata.get("target_employee_count") or "").strip()
        target_industry = (campaign_metadata.get("target_industry") or "").strip()
        campaign_prompt = _as_text(
            campaign_metadata.get("prompt") or campaign_metadata.get("campaign_prompt"),
            empty="Not specified",
        )

        sender_customers = _as_text(
            user_intel.get("target_customers") or user_intel.get("customers")
        )
        sender_services = _as_text(
            user_intel.get("services")
            or user_intel.get("offerings")
            or user_intel.get("core_offerings")
        )

        # ── Check 1: Location ────────────────────────────────────────────────
        loc_v, loc_e = match_location(csv_country, target_loc)
        location_check = CheckResult(verdict=loc_v, evidence=loc_e)

        # ── Check 2: Size ────────────────────────────────────────────────────
        sz_v, sz_e = match_size(csv_size, target_size)
        size_check = CheckResult(verdict=sz_v, evidence=sz_e)

        # ── Check 3: Industry (LLM — always runs) ───────────────────────────
        from app.core.logging_config import agent_label_var, company_domain_var
        _dom_tok = company_domain_var.set(company_data.get("domain") or company_data.get("website") or name)
        _ag_tok  = agent_label_var.set("screener")
        tok_usage: dict = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        try:
            if _is_blank(target_industry):
                industry_check = CheckResult(verdict="pass", evidence="No target industry specified.")
            else:
                sys_msg = self.build_system_message(
                    target_industry=target_industry,
                    sender_customers=sender_customers,
                    sender_services=sender_services,
                    campaign_prompt=campaign_prompt,
                )
                industry_check, tok_usage = await self._check_industry(
                    name=name,
                    csv_industry=csv_industry,
                    csv_desc=csv_desc,
                    csv_sic=csv_sic,
                    csv_naics=csv_naics,
                    system_message=sys_msg,
                )
        finally:
            agent_label_var.reset(_ag_tok)
            company_domain_var.reset(_dom_tok)

        # ── Final verdict ────────────────────────────────────────────────────
        failed: list[str] = []
        if location_check.verdict == "fail":
            failed.append("location")
        if size_check.verdict == "fail":
            failed.append("size")
        if industry_check.verdict == "fail":
            failed.append("industry")

        final = "FAIL" if failed else "PASS"

        if final == "FAIL":
            summary = f"FAIL on: {', '.join(failed)}. " + " | ".join(
                f"{c}: {r.evidence}"
                for c, r in [
                    ("location", location_check),
                    ("size", size_check),
                    ("industry", industry_check),
                ]
                if r.verdict == "fail"
            )
        else:
            parts = []
            for label, check in [("location", location_check), ("size", size_check), ("industry", industry_check)]:
                parts.append(f"{label}={check.verdict}")
            summary = "PASS — " + ", ".join(parts)

        logger.info(f"[LeadFitScreener] {name}: {final} — {summary}")

        return ScreenerOutput(
            company_name=name,
            location_check=location_check,
            size_check=size_check,
            industry_check=industry_check,
            final_verdict=final,
            failed_checks=failed,
            summary=summary,
            token_usage=tok_usage,
        )

    @staticmethod
    def build_system_message(
        target_industry: str,
        sender_customers: str,
        sender_services: str,
        campaign_prompt: str,
    ) -> str:
        """
        Combine static rules + ALL campaign-level context into one system message.
        This string is IDENTICAL for every company in the same batch, so OpenAI
        caches the entire prefix after the first call — only the company block
        (sent as HumanMessage) incurs uncached token cost.
        """
        return (
            _INDUSTRY_SCREEN_SYSTEM
            + "\n\n"
            + "=" * 60 + "\n"
            + "CAMPAIGN CONTEXT — same for every company in this batch:\n\n"
            + "SENDER CONTEXT (for industry boundary reference only):\n"
            + f"- Sender sells to: {sender_customers}\n"
            + f"- Sender's offerings: {sender_services}\n\n"
            + "CAMPAIGN OBJECTIVE (the specific goal the campaign is targeting — use this to\n"
            + "understand the operational context and what kind of companies are sought beyond\n"
            + "the industry label alone):\n"
            + f"{campaign_prompt[:500]}\n\n"
            + f"TARGET INDUSTRY: {target_industry}\n\n"
            + "Classify the company supplied in the next message."
        )

    async def _check_industry(
        self,
        name: str,
        csv_industry: str,
        csv_desc: str,
        csv_sic: str,
        csv_naics: str,
        system_message: str,
    ) -> tuple["CheckResult", dict]:
        """
        Industry LLM call. Returns (CheckResult, token_usage_dict).

        Uses include_raw=True so we get the raw AIMessage alongside the parsed
        result — AIMessage.usage_metadata contains input_token_details.cached
        which is the only reliable way to read cached token counts from LangChain.
        """
        sic_naics_ctx = _sic_naics_context(csv_sic, csv_naics)
        description = csv_desc.strip() if csv_desc and not _is_blank(csv_desc) else "(no description provided)"

        human_content = (
            f"COMPANY:\n"
            f"Name: {name}\n"
            f"Industry label: {csv_industry or 'Not stated'}\n"
            f"SIC/NAICS: {sic_naics_ctx}\n"
            f'Description: """{description[:2000]}"""'
        )

        _empty_tok: dict = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

        try:
            messages = [
                SystemMessage(content=system_message),
                HumanMessage(content=human_content),
            ]
            chain = self._llm.with_structured_output(IndustryScreenVerdict, include_raw=True)
            raw_output: dict = await chain.ainvoke(messages)

            parsed: IndustryScreenVerdict = raw_output["parsed"]
            ai_msg = raw_output.get("raw")

            # Read token usage from the raw AIMessage response_metadata.
            # usage_metadata only has input/output totals; cached_tokens lives in
            # response_metadata["token_usage"]["prompt_tokens_details"]["cached_tokens"].
            tok = dict(_empty_tok)
            if ai_msg is not None:
                usage_meta = getattr(ai_msg, "usage_metadata", None) or {}
                tok["input_tokens"]  = usage_meta.get("input_tokens", 0) or 0
                tok["output_tokens"] = usage_meta.get("output_tokens", 0) or 0
                rm = getattr(ai_msg, "response_metadata", None) or {}
                token_usage = rm.get("token_usage") or {}
                details = token_usage.get("prompt_tokens_details") or {}
                tok["cached_tokens"] = details.get("cached_tokens", 0) or 0

            check = CheckResult(
                verdict=parsed.verdict,
                evidence=f"[{parsed.actual_business}] {parsed.evidence}",
            )
            return check, tok

        except Exception as e:
            logger.warning(f"[LeadFitScreener] Industry LLM failed for '{name}': {e}")
            return CheckResult(verdict="unknown", evidence=f"Industry check could not complete: {e}"), _empty_tok


# --------------------------------------------------------------------------- #
# Utilities                                                                    #
# --------------------------------------------------------------------------- #

def _as_text(value, empty: str = "Not specified") -> str:
    if value is None:
        return empty
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(items) if items else empty
    text = str(value).strip()
    if not text or _is_blank(text):
        return empty
    parts = [p.strip() for p in re.split(r"[,;\n]+", text) if p.strip()]
    return ", ".join(parts) if parts else empty
