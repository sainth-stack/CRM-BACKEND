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
            "'pass' = company's primary business clearly belongs to the target industry. "
            "'fail' = company's primary business clearly does NOT belong to the target industry. "
            "'unknown' = signals are too thin, missing, or genuinely contradictory to decide."
        )
    )
    decision_reason: str = Field(
        description="One sentence: the single clearest reason why this company was approved, rejected, or marked inconclusive."
    )
    industry_label_signal: str = Field(
        description="State the industry label and whether it aligns with, conflicts with, or is unrelated to the target industry."
    )
    sic_signal: str = Field(
        description="State the SIC code number, its official category name, and whether it aligns with or conflicts with the target industry. If not provided, say 'SIC: not provided'."
    )
    naics_signal: str = Field(
        description="State the NAICS code number, its official category name, and whether it aligns with or conflicts with the target industry. If not provided, say 'NAICS: not provided'."
    )
    description_signal: str = Field(
        description="Quote the single most relevant phrase from the company description and state what it confirms or contradicts about industry fit."
    )
    signals_conclusion: str = Field(
        description="State whether all signals agree or conflict. If they conflict, state which signal is most reliable and why it was trusted over the others."
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
# Geography helpers                                                            #
# --------------------------------------------------------------------------- #

import pycountry
import pycountry_convert as pc

_BLANK_VALUES = {"", "n/a", "na", "none", "null", "unknown", "any", "-"}

# All continent names returned by pycountry-convert, lowercased for fast lookup.
_CONTINENT_NAMES: frozenset[str] = frozenset({
    "africa", "antarctica", "asia", "europe",
    "north america", "oceania", "south america",
})


def _is_blank(v: str) -> bool:
    return (v or "").strip().lower() in _BLANK_VALUES


def _resolve_country(name: str):
    """Return a pycountry country object or None. Tries lookup() then search_fuzzy()."""
    try:
        return pycountry.countries.lookup(name.strip())
    except LookupError:
        pass
    try:
        results = pycountry.countries.search_fuzzy(name.strip())
        if results:
            return results[0]
    except LookupError:
        pass
    return None


def _prospect_continent(alpha2: str) -> str | None:
    """Return the continent name (lowercase) for a country alpha-2, or None."""
    try:
        code = pc.country_alpha2_to_continent_code(alpha2)
        return pc.convert_continent_code_to_continent_name(code).lower()
    except (KeyError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Check 1: Location                                                            #
# --------------------------------------------------------------------------- #

def match_location(csv_country: str, target_loc: str) -> tuple[str, str]:
    """
    Deterministic location match. Target is always a Country OR Continent.
    Prospect (csv_country) is always a Country.

    Resolution order:
      1. If target matches a known continent name → continent check.
      2. Otherwise resolve target as a country → direct country equality check.

    Continent is checked FIRST to prevent pycountry fuzzy-matching continent
    names to countries (e.g. "Africa" → "South Africa").

    Returns (verdict, evidence):
      'pass'    — match confirmed.
      'fail'    — prospect country is known and does not match.
      'unknown' — missing data or unresolvable name (safe pass-through).
    """
    if _is_blank(target_loc):
        return "pass", "No location requirement specified."
    if _is_blank(csv_country):
        return "unknown", "Company country not stated in CSV."

    prospect = _resolve_country(csv_country)
    if prospect is None:
        return "unknown", f"Could not resolve prospect country '{csv_country}'."

    target_norm = target_loc.strip().lower()

    # ── Case 1: Target is a continent ───────────────────────────────────────
    if target_norm in _CONTINENT_NAMES:
        prospect_continent = _prospect_continent(prospect.alpha_2)
        if prospect_continent is None:
            return "unknown", (
                f"Could not determine continent for '{csv_country}' ({prospect.alpha_2})."
            )
        if prospect_continent == target_norm:
            return "pass", (
                f"'{csv_country}' is in {prospect_continent.title()}, "
                f"which matches target '{target_loc}'."
            )
        return "fail", (
            f"'{csv_country}' is in {prospect_continent.title()}, "
            f"not in target continent '{target_loc}'."
        )

    # ── Case 2: Target is a country ─────────────────────────────────────────
    target_country = _resolve_country(target_loc)
    if target_country is not None:
        if prospect.alpha_2 == target_country.alpha_2:
            return "pass", f"'{csv_country}' matches target country '{target_loc}'."
        return "fail", (
            f"'{csv_country}' ({prospect.alpha_2}) does not match "
            f"target country '{target_loc}' ({target_country.alpha_2})."
        )

    return "unknown", f"Could not resolve target location '{target_loc}' as a country or continent."


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
  - The industry label, SIC/NAICS codes, and description consistently confirm
    membership in that sector.

"fail" — the company clearly does NOT belong to the target industry:
  - Its core activities have no meaningful overlap with the target industry.
  - The company's industry label AND description both point away from the target sector.
  - A company that serves or sells to companies in the target industry is not
    automatically a fail — evaluate what the company itself primarily does.

"unknown" — the available signals are too thin or conflicting to decide:
  - Description is very short, generic, or uses only vague terms.
  - The company could plausibly belong to the target industry or to another sector.
  - Industry label and description give conflicting or unclear signals.
  - The company genuinely straddles two industry categories.

CALIBRATION RULES:
  1. Follow the evidence — do not invent reasons to pass or fail. Let the industry
     label, SIC/NAICS code, and description speak for themselves.
  2. UNKNOWN is a pass-through — use it only when the signals are genuinely
     insufficient or contradictory, not as a default to avoid making a call.
  3. SIC/NAICS codes are supporting evidence. State what the code means and whether
     it aligns or conflicts with the description. Source data sometimes assigns
     incorrect codes — if the code and description clearly contradict each other,
     explain the conflict and state which signal is more reliable and why.
  4. If all three signals (industry label, SIC/NAICS, description) agree, the verdict
     should match them — pass if they all point to the target industry, fail if they
     all point away from it.
  5. Only use UNKNOWN when at least one signal is missing or signals genuinely conflict
     with no clear way to resolve the contradiction.
  6. Treat the target industry as a BROAD SUPERCATEGORY. Any company whose primary
     business falls within a recognised subcategory of the target industry is a PASS.
     Do not require an exact label match — classify by what the company actually does,
     not by whether the wording of its label exactly matches the target industry name.

REASONING FIELDS — fill each field precisely:

decision_reason   : One sentence — the single clearest reason the company was approved, rejected, or marked inconclusive.
industry_label_signal : State the label and whether it aligns with, conflicts with, or is unrelated to the target industry.
sic_signal        : State the SIC code number and its official category name, then state whether it aligns or conflicts with the target industry.
naics_signal      : State the NAICS code number and its official category name, then state whether it aligns or conflicts with the target industry.
description_signal: Quote the single most relevant phrase from the description verbatim, then state what it confirms or contradicts.
signals_conclusion: State whether all signals agree or conflict. If conflict, name which signal is most reliable and why.
\
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

        # ── Check 3: Industry (LLM) ──────────────────────────────────────────
        # Short-circuit: if location or size is already a hard FAIL the final
        # verdict is FAIL regardless of industry — skip the LLM call entirely.
        _already_rejected = (
            location_check.verdict == "fail" or size_check.verdict == "fail"
        )

        from app.core.logging_config import agent_label_var, company_domain_var
        _dom_tok = company_domain_var.set(company_data.get("domain") or company_data.get("website") or name)
        _ag_tok  = agent_label_var.set("screener")
        tok_usage: dict = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        try:
            if _already_rejected:
                industry_check = CheckResult(
                    verdict="unknown",
                    evidence="Skipped — company already rejected on location/size.",
                )
            elif _is_blank(target_industry):
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
            + f"- Sender sells to: {sender_customers[:300]}\n"
            + f"- Sender's offerings: {sender_services[:300]}\n\n"
            + "CAMPAIGN OBJECTIVE (use this only to understand the industry boundary —\n"
            + "do not use it to stretch or narrow the target industry beyond what the\n"
            + "TARGET INDUSTRY label already states):\n"
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

            decision_word = {"pass": "APPROVED", "fail": "REJECTED", "unknown": "INCONCLUSIVE"}.get(parsed.verdict, parsed.verdict.upper())
            evidence = (
                f"DECISION: {decision_word} — {parsed.decision_reason}\n"
                f"- Industry label: {parsed.industry_label_signal}\n"
                f"- {parsed.sic_signal}\n"
                f"- {parsed.naics_signal}\n"
                f"- Description: {parsed.description_signal}\n"
                f"- Signals: {parsed.signals_conclusion}"
            )
            check = CheckResult(
                verdict=parsed.verdict,
                evidence=evidence,
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
