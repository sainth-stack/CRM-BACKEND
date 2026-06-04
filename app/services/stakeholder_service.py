import re
import logging
from typing import List, Dict, Any

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.llm import get_chat_llm

logger = logging.getLogger("StakeholderRanking")

# How many contacts (best-by-deterministic-prescore) get the AI persona-fit pass.
# The prescore ranks the FULL contact set first, so this is a cost cap on the
# LLM call — never an arbitrary "first N rows" truncation like the old code.
SHORTLIST_SIZE = 15

# Email-validation tokens that mean "deliverable".
_VALID_TOKENS = {"valid", "accept all", "accept_all", "deliverable", "ok", "safe"}
# Tokens that mean "do not use this address".
_INVALID_TOKENS = {"invalid", "undeliverable", "bad", "do_not_mail", "do not mail", "bounced"}

# Seniority signal from the `seniority` column and/or title keywords.
_SENIORITY_WEIGHTS = [
    (r"\b(c-?level|chief|cxo|ceo|cfo|coo|cto|cmo|ciso|founder|owner|president|partner)\b", 100),
    (r"\b(vp|vice\s*president|svp|evp)\b", 90),
    (r"\b(head|director)\b", 80),
    (r"\b(principal|lead|senior\s*manager)\b", 65),
    (r"\b(manager|management)\b", 55),
    (r"\b(senior|specialist|architect)\b", 45),
]


class RankedContact(BaseModel):
    index: int = Field(description="The contact's index number from the provided list.")
    role_fit: int = Field(description="0-100: likelihood this person is a decision-maker/economic buyer/champion for the sender's solution.")
    reasoning: str = Field(description="One sentence on why this role fits (or doesn't).")


class StakeholderRanking(BaseModel):
    rankings: List[RankedContact] = Field(description="A ranking entry for EVERY contact index provided.")


class StakeholderRankingService:
    """Selects the strongest stakeholders per company.

    Phase 3: ranks the FULL contact set (not the first 15 by CSV order), scores
    real reachability from the email-validation columns, and judges role/persona
    fit against the sender DNA + deep research + campaign objective in a single
    batched LLM call (cheap model). Returns the full list sorted best-first; the
    caller takes the top N.
    """

    def __init__(self):
        self.llm = get_chat_llm("cheap")

    # ------------------------------------------------------------------ #
    # Deterministic signals                                              #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _best_email(p: Dict[str, Any]) -> tuple[str | None, bool]:
        """Return (best_email, is_verified) using primary + the 10 validated slots."""
        primary = p.get("primary_email")
        if primary:
            return str(primary), True  # primary is the curated/known-good address
        for i in range(1, 11):
            email = p.get(f"email_{i}")
            if not email:
                continue
            status = str(p.get(f"email_{i}_validation") or "").strip().lower()
            if status in _VALID_TOKENS:
                return str(email), True
        # Fall back to any present email that is NOT explicitly invalid (unknown
        # validation is acceptable; a known-bad address is not).
        for i in range(1, 11):
            email = p.get(f"email_{i}")
            if not email:
                continue
            status = str(p.get(f"email_{i}_validation") or "").strip().lower()
            if status in _INVALID_TOKENS:
                continue
            return str(email), False
        return None, False

    @staticmethod
    def _reachability(p: Dict[str, Any]) -> int:
        email, verified = StakeholderRankingService._best_email(p)
        if not email:
            return 0
        return 100 if verified else 50

    @staticmethod
    def _seniority_score(p: Dict[str, Any]) -> int:
        text = " ".join(
            str(p.get(k) or "") for k in ("seniority", "title", "department")
        ).lower()
        for pattern, weight in _SENIORITY_WEIGHTS:
            if re.search(pattern, text):
                return weight
        return 30

    # ------------------------------------------------------------------ #
    # Main entry                                                          #
    # ------------------------------------------------------------------ #
    async def rank_stakeholders_with_ai(
        self,
        prospects: List[Dict[str, Any]],
        user_intel: dict,
        research_context: str = "",
        objective: str = "",
    ) -> List[Dict[str, Any]]:
        """Rank ALL prospects for a company, best-first. Each returned dict gains
        `strategic_score`, `strategic_reasoning`, `reachability`."""
        if not prospects:
            return []

        # 1. Drop the unreachable, attach deterministic signals to the rest.
        candidates: List[Dict[str, Any]] = []
        for p in prospects:
            reach = self._reachability(p)
            if reach == 0:
                continue  # no usable email — cannot be contacted
            p["reachability"] = reach
            p["_seniority_score"] = self._seniority_score(p)
            candidates.append(p)

        if not candidates:
            return []

        # 2. Prescore over the FULL set, then shortlist the strongest for AI.
        candidates.sort(
            key=lambda c: 0.5 * c["reachability"] + 0.5 * c["_seniority_score"],
            reverse=True,
        )
        shortlist = candidates[:SHORTLIST_SIZE]

        # 3. One batched persona-fit call for the whole shortlist.
        role_fits = await self._score_role_fit(shortlist, user_intel, research_context, objective)

        for idx, p in enumerate(shortlist):
            fit = role_fits.get(idx, {})
            role_fit = fit.get("role_fit", p["_seniority_score"])  # fall back to seniority
            p["strategic_score"] = round(0.7 * role_fit + 0.3 * p["reachability"], 2)
            p["strategic_reasoning"] = fit.get("reasoning") or "Ranked by reachability and seniority."

        # Contacts beyond the shortlist keep a deterministic-only score so they
        # remain selectable if a company has fewer verified leads than expected.
        for p in candidates[SHORTLIST_SIZE:]:
            p["strategic_score"] = round(0.5 * p["reachability"] + 0.5 * p["_seniority_score"], 2)
            p["strategic_reasoning"] = "Ranked by reachability and seniority (outside AI shortlist)."

        candidates.sort(key=lambda c: c["strategic_score"], reverse=True)
        return candidates

    async def _score_role_fit(
        self, shortlist, user_intel, research_context, objective
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

        prompt = ChatPromptTemplate.from_template(
            """You are a B2B sales strategist selecting which people to contact at a target account.

SENDER'S IDEAL BUYER PROFILES: {target_profiles}
PAINS THE SENDER SOLVES: {pains_solved}
CAMPAIGN OBJECTIVE (the user's goal — prioritise people who match it): {objective}
TARGET COMPANY RESEARCH: {research_context}

CONTACTS (score EVERY index):
{contacts_block}

For each contact, return role_fit (0-100) = how likely this person is the
decision-maker, economic buyer, or internal champion for the sender's solution
given their title/seniority/department and the campaign objective. Favour people
with authority over the relevant function; down-rank clearly irrelevant roles.
Return a ranking entry for every index above."""
        )

        structured = self.llm.with_structured_output(StakeholderRanking)
        chain = prompt | structured
        try:
            result: StakeholderRanking = await chain.ainvoke({
                "target_profiles": str(target_profiles)[:1500],
                "pains_solved": str(pains_solved)[:1500],
                "objective": (objective or "(general outreach)")[:1000],
                "research_context": (research_context or "N/A")[:2000],
                "contacts_block": contacts_block,
            })
            return {
                r.index: {"role_fit": max(0, min(100, r.role_fit)), "reasoning": r.reasoning}
                for r in result.rankings
            }
        except Exception as e:
            logger.error(f"[STAKEHOLDER] Batched role-fit scoring failed: {e}")
            return {}
