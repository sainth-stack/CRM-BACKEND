import logging
from typing import List, Dict, Any, Literal

from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

from app.core.llm import get_chat_llm

logger = logging.getLogger("StakeholderRanking")

MAX_CONTACTS_PER_CALL = 60

_COMPANY_FIT_CEILING = {"strong": 100, "partial": 65, "weak": 30}


def _apply_ceiling(raw_scores: dict[int, dict], ceiling: int) -> dict[int, dict]:
    """Apply a company-fit ceiling while preserving relative spread.

    Simple clamp (min(ceiling, score)) collapses all contacts to the ceiling value
    whenever they all score above it — destroying the ordering. Instead we
    proportionally scale the full range [0 → max_raw] → [0 → ceiling], so the
    relative differences between contacts are always preserved.
    """
    if not raw_scores:
        return raw_scores
    max_raw = max(v["role_fit"] for v in raw_scores.values())
    if max_raw <= 0:
        return {k: {**v, "role_fit": 0} for k, v in raw_scores.items()}
    if max_raw <= ceiling:
        # All scores already within the ceiling — just clamp negatives.
        return {k: {**v, "role_fit": max(0, v["role_fit"])} for k, v in raw_scores.items()}
    # Proportional scaling: score * (ceiling / max_raw), rounded to nearest int.
    factor = ceiling / max_raw
    return {
        k: {**v, "role_fit": max(0, round(v["role_fit"] * factor))}
        for k, v in raw_scores.items()
    }


class RankedContact(BaseModel):
    index: int = Field(description="The contact's index number from the provided list.")
    role_fit: int = Field(
        description=(
            "0-100 score BEFORE the company-fit ceiling is applied. Score each person purely "
            "on how well their function and seniority match the economic buyer / champion / "
            "influencer profile for this deal. The system enforces the ceiling in code — you "
            "do NOT need to cap scores yourself here. Spread scores — never the same value twice."
        )
    )
    reasoning: str = Field(
        description=(
            "ONE professional, forward-facing sentence about THIS person — what they do, "
            "what influence they likely have over this purchase, and how to engage them "
            "(or why they are not the primary target). Write for a salesperson in natural "
            "business language. NEVER use internal scoring terms: no 'pillar', 'ceiling', "
            "'cap', 'viability', 'function fit', 'irrelevant function', 'aligns with', "
            "'does not align', 'buying committee', 'STEP', or quoted rubric labels."
        )
    )


class StakeholderRanking(BaseModel):
    company_fit: Literal["strong", "partial", "weak"] = Field(
        description=(
            "Company-offer fit verdict from STEP B. "
            "'strong' = company clearly operates in the domain the sender's offering addresses. "
            "'partial' = company has some but not all ideal-customer characteristics. "
            "'weak'   = company core activities don't align with what the sender solves."
        )
    )
    rankings: List[RankedContact] = Field(description="A ranking entry for EVERY contact index provided.")


class StakeholderRankingService:
    """Ranks the strongest stakeholders per company for a given campaign.

    Reachability (primary email present) is the only deterministic filter.
    All scoring is fully dynamic — derived from sender profile, campaign objective,
    and per-company context. No hardcoded industry or offer-type rubrics.
    """

    def __init__(self):
        self.llm = get_chat_llm("cheap", timeout=60, max_retries=1)

    # ------------------------------------------------------------------ #
    # Reachability                                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _best_email(p: Dict[str, Any]) -> tuple[str | None, bool]:
        primary = p.get("primary_email")
        if primary:
            return str(primary), True
        return None, False

    @staticmethod
    def _reachability(p: Dict[str, Any]) -> int:
        email, _ = StakeholderRankingService._best_email(p)
        return 100 if email else 0

    # ------------------------------------------------------------------ #
    # Main entry                                                           #
    # ------------------------------------------------------------------ #
    async def rank_stakeholders_with_ai(
        self,
        prospects: List[Dict[str, Any]],
        user_intel: dict,
        research_context: str = "",
        objective: str = "",
        industry: str = "",
    ) -> List[Dict[str, Any]]:
        """Rank all reachable prospects for a company, best-first.

        STAGE 1 (deterministic) — drop anyone without a Primary Email.
        STAGE 2 (fully dynamic) — LLM scores every remaining contact 0-100
          from sender profile, campaign objective, company research, and
          per-contact title/seniority/department/tenure. No hardcoded rubric.
        """
        if not prospects:
            return []

        candidates = [p for p in prospects if self._reachability(p) == 100]
        for p in candidates:
            p["reachability"] = 100
        if not candidates:
            return []

        scored = candidates[:MAX_CONTACTS_PER_CALL]
        role_fits = await self._score_role_fit(scored, user_intel, research_context, objective, industry)

        for idx, p in enumerate(scored):
            fit = role_fits.get(idx)
            if fit:
                p["strategic_score"] = fit["role_fit"]
                p["strategic_reasoning"] = fit["reasoning"]
            else:
                p["strategic_score"] = 50
                p["strategic_reasoning"] = "Scored neutrally — automated assessment was unavailable."

        for p in candidates[MAX_CONTACTS_PER_CALL:]:
            p["strategic_score"] = 50
            p["strategic_reasoning"] = "Scored neutrally — beyond the per-company scoring batch."

        candidates.sort(key=lambda c: c["strategic_score"], reverse=True)
        return candidates

    # ------------------------------------------------------------------ #
    # Prompt templates — split for prompt caching                         #
    #                                                                     #
    # SystemMessage  : static methodology (never changes)                 #
    # HumanMessage 1 : campaign-level sender profile (same per campaign)  #
    # HumanMessage 2 : per-company inputs (changes every call)            #
    # ------------------------------------------------------------------ #
    _SYSTEM = """\
You are a B2B sales strategist deciding WHO to contact at a target account.
Score each contact 0-100 on how well they fit as the ECONOMIC BUYER, CHAMPION, or
key INFLUENCER for what this sender is offering. Every score must be earned from
the evidence — derive everything from the sender profile and company context below.

════════════════════════════════════════════════════════
SCORING METHODOLOGY — apply all three in order
════════════════════════════════════════════════════════

STEP A — DERIVE THE DECISION-MAKER PROFILE FOR THIS DEAL
From the campaign objective and sender profile, work out:
  (i)  Who OWNS the budget for this type of purchase?
  (ii) Who will CHAMPION it internally (drives evaluation, builds the business case)?
  (iii) Who are strong INFLUENCERS (consulted, but don't own the final call)?

Do this derivation first — DO NOT assume fixed answers. The economic buyer for a
maintenance-software deal at a manufacturer is different from the economic buyer for
the same software at a logistics firm. Derive from what this specific company does.

STEP B — COMPANY-OFFER FIT
Determine how closely the sender's offering maps to this company's actual operations.
Base this entirely on the campaign objective + company industry + research.

Output ONE of these three verdicts in the `company_fit` field:
  "strong" — the company clearly operates in the domain the sender's offering addresses.
  "partial" — the company has some but not all ideal-customer characteristics.
  "weak"   — the company's core activities don't align with what the sender solves.

The system code enforces the score ceiling automatically based on your verdict:
  strong → 100 max  |  partial → 65 max  |  weak → 30 max
You do NOT need to cap scores in the role_fit field — score each person 0-100 based
purely on their function and seniority, and the ceiling is applied in code after.

State your fit verdict in the `company_fit` field, then score contacts in `rankings`.

STEP C — SCORE EACH CONTACT INDIVIDUALLY (primary driver: function, then seniority)
Score 0-100 for each contact based purely on function + seniority fit for this deal.
The system applies the company-fit ceiling from STEP B in code — you do not cap here.

Function-fit categories:
  ECONOMIC BUYER — this person owns the budget/decision for this purchase:   75-100
  CHAMPION       — right function, strong internal influence, not final owner: 50-74
  INFLUENCER     — adjacent function, will be consulted, doesn't own it:      25-49
  PERIPHERAL     — function clearly doesn't touch this decision here:          0-24

TITLE INTERPRETATION RULES (context is everything — never read a title in isolation):
  • Interpret every title in the context of what THIS company actually does.
  • "Director of Operations" means production/plant at a manufacturer, engineering
    delivery at a software firm, project delivery at a consulting firm, store/supply
    chain at retail — derive from company type, don't assume.
  • "Sales Operations" is about sales process (CRM, pipeline tools) — low fit for
    offers that target production, finance, or engineering functions.
  • "Engineering" at a manufacturer is ADJACENT (not peripheral): at a machinery or
    equipment maker, engineering owns product design and often co-owns tooling and
    production readiness decisions — score as INFLUENCER (25-49) unless the campaign
    is specifically about engineering tools, in which case they may be economic buyer.
  • Functions like HR, Legal, Finance, Marketing score PERIPHERAL for operational
    tools unless the campaign explicitly targets those functions.
  • A senior title in the WRONG function scores LOW — seniority rescues nothing
    when the function is peripheral to this purchase.

Seniority modifier (applied within the correct function only):
  C-suite (CEO/COO/CFO/CTO/etc.) → +15
  VP / SVP / EVP                 → +12
  Director / Senior Director     → +8
  Manager / Senior Manager       → +4
  Individual Contributor         → +0
  NEW in role (<12 months)       → additional +5 (active buying-window signal)

CALIBRATION:
  85-100 = Economic buyer, clear budget authority, right function, senior.
  65-84  = Strong champion or VP-level influencer in the right function.
  40-64  = Influencer or director-level adjacent function; worth a CC or warm intro.
  0-39   = Function peripheral to this decision, or company fit too weak to prioritise.

SPREAD REQUIREMENT: scores across the contact list MUST spread. Never assign the same
score to two contacts unless every relevant dimension (function, seniority, tenure, fit)
is truly identical. Two contacts with different titles or departments must score
differently — even if the gap is small.

════════════════════════════════════════════════════════
REASONING RULES
════════════════════════════════════════════════════════
Write ONE professional, forward-facing sentence per contact. State what this person
actually does, what influence they likely have over this specific purchase, and the
best engagement angle — OR why they are not the primary target and who would be better.

Write for a salesperson, in natural business language.
BANNED phrases (expose the rubric — never write these):
  "irrelevant function", "aligns with", "does not align with", "buying committee",
  "key functions", "viability", "not viable", "pillar", "ceiling", "cap", "STEP",
  "function fit", "economic buyer profile", "scoring rubric", "band", "score".\
"""

    _CAMPAIGN = """\
════════════════════════════════════════════════════════
SENDER PROFILE
════════════════════════════════════════════════════════
Ideal buyer profiles:  {target_profiles}
Pains the sender solves: {pains_solved}
Campaign objective:    {objective}\
"""

    _TARGET = """\
════════════════════════════════════════════════════════
TARGET COMPANY (the only per-company inputs)
════════════════════════════════════════════════════════
Industry: {industry}
Research: {research_context}

CONTACTS — score EVERY index 0-100 (the system applies the ceiling from STEP B in code):
{contacts_block}

First output the `company_fit` verdict, then a ranking entry for EVERY index.\
"""

    # ------------------------------------------------------------------ #
    # Scoring                                                              #
    # ------------------------------------------------------------------ #
    async def _score_role_fit(
        self, shortlist, user_intel, research_context, objective, industry=""
    ) -> Dict[int, dict]:
        target_profiles = user_intel.get("target_customers", [])
        pains_solved = user_intel.get("capability_to_pain_map", [])

        contact_lines = []
        for idx, p in enumerate(shortlist):
            contact_lines.append(
                f"{idx}. Name: {p.get('contact_full_name', 'N/A')} | "
                f"Title: {p.get('title') or p.get('position') or 'N/A'} | "
                f"Seniority: {p.get('seniority', 'N/A')} | "
                f"Department: {p.get('department', 'N/A')} | "
                f"Time in role: {p.get('time_in_role', 'N/A')} | "
                f"Time at company: {p.get('time_at_company', 'N/A')}"
            )
        contacts_block = "\n".join(contact_lines)

        messages = [
            SystemMessage(content=self._SYSTEM),
            HumanMessage(content=self._CAMPAIGN.format(
                target_profiles=str(target_profiles)[:1500],
                pains_solved=str(pains_solved)[:1500],
                objective=(objective or "(general outreach)")[:1000],
            )),
            HumanMessage(content=self._TARGET.format(
                industry=(industry or "(unspecified)")[:300],
                research_context=(research_context or "N/A")[:2000],
                contacts_block=contacts_block,
            )),
        ]

        structured = self.llm.with_structured_output(StakeholderRanking)
        try:
            result: StakeholderRanking = await structured.ainvoke(messages)
            ceiling = _COMPANY_FIT_CEILING.get(result.company_fit, 100)
            logger.info(f"[STAKEHOLDER] company_fit={result.company_fit} ceiling={ceiling}")
            raw = {
                r.index: {"role_fit": max(0, r.role_fit), "reasoning": r.reasoning}
                for r in result.rankings
            }
            return _apply_ceiling(raw, ceiling)
        except Exception as e:
            logger.error(f"[STAKEHOLDER] Batched role-fit scoring failed: {e}")
            return {}
