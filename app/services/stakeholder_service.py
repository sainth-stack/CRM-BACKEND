import logging
from typing import List, Dict, Any

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.llm import get_chat_llm

logger = logging.getLogger("StakeholderRanking")

# Safety cap on how many contacts go into a single batched scoring call (guards the
# prompt size for a pathologically large contact list). Effectively "all" for normal
# companies. NOT a quality shortlist — every contact within this cap is AI-scored.
MAX_CONTACTS_PER_CALL = 60


class RankedContact(BaseModel):
    index: int = Field(description="The contact's index number from the provided list.")
    role_fit: int = Field(description="0-100: likelihood this person is a decision-maker/economic buyer/champion for the sender's solution.")
    reasoning: str = Field(
        description=(
            "ONE professional, customer-facing sentence on the engagement decision for THIS person — "
            "grounded in their actual title/function and the deal context. Speak about the PERSON and the "
            "decision (whether/how to engage them, what influence they have, who would be a better entry "
            "point if any), NEVER recite the rubric, the buying-committee functions, the seniority bands, "
            "or any internal scoring framework. Do NOT use phrases like 'irrelevant function', 'aligns with "
            "key functions', 'does not align with', 'matched to committee', or quoted department names like "
            "'Other'. Write as if explaining to a sales rep, in natural business language."
        )
    )


class StakeholderRanking(BaseModel):
    rankings: List[RankedContact] = Field(description="A ranking entry for EVERY contact index provided.")


class StakeholderRankingService:
    """Selects the strongest stakeholders per company.

    Phase 3: ranks the FULL contact set (not the first 15 by CSV order), scores
    reachability by whether a Primary Email exists, and judges role/persona fit
    against the sender DNA + deep research + campaign objective in a single batched
    LLM call (cheap model). Returns the full list sorted best-first; the caller
    takes the top N.
    """

    def __init__(self):
        # Cap tail latency (60s x 1 retry): one batched call per company, run
        # concurrently across the chunk — a slow provider must not stall the stage.
        self.llm = get_chat_llm("cheap", timeout=60, max_retries=1)

    # ------------------------------------------------------------------ #
    # Deterministic signals                                              #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _best_email(p: Dict[str, Any]) -> tuple[str | None, bool]:
        """Return (primary_email, exists). Reachability is decided solely by the
        presence of a Primary Email — the per-slot validation columns were removed."""
        primary = p.get("primary_email")
        if primary:
            return str(primary), True
        return None, False

    @staticmethod
    def _reachability(p: Dict[str, Any]) -> int:
        email, _ = StakeholderRankingService._best_email(p)
        return 100 if email else 0

    # ------------------------------------------------------------------ #
    # Main entry                                                          #
    # ------------------------------------------------------------------ #
    async def rank_stakeholders_with_ai(
        self,
        prospects: List[Dict[str, Any]],
        user_intel: dict,
        research_context: str = "",
        objective: str = "",
        industry: str = "",
    ) -> List[Dict[str, Any]]:
        """Rank ALL reachable prospects for a company, best-first. Each returned dict
        gains `strategic_score` (0-100) and `strategic_reasoning`.

        Two stages:
          STAGE 1 (deterministic) — drop anyone without a Primary Email.
          STAGE 2 (fully dynamic, agent-decided) — the LLM scores EVERY remaining
            prospect 0-100 from the campaign objective + the target industry + that
            prospect's own title/seniority/department/tenure. No hardcoded seniority
            table, no shortlist, no reachability blend; the agent's score IS the
            strategic score, so it varies per prompt + industry + person."""
        if not prospects:
            return []

        # STAGE 1 — reachability filter (the ONLY deterministic step).
        candidates = [p for p in prospects if self._reachability(p) == 100]
        for p in candidates:
            p["reachability"] = 100
        if not candidates:
            return []

        # STAGE 2 — dynamic AI scoring of every reachable prospect (one batched call).
        scored = candidates[:MAX_CONTACTS_PER_CALL]
        role_fits = await self._score_role_fit(scored, user_intel, research_context, objective, industry)

        for idx, p in enumerate(scored):
            fit = role_fits.get(idx)
            if fit:
                p["strategic_score"] = fit["role_fit"]
                p["strategic_reasoning"] = fit["reasoning"]
            else:
                # AI-unavailable fallback: neutral score so the prospect stays
                # selectable — never a hardcoded seniority ranking.
                p["strategic_score"] = 50
                p["strategic_reasoning"] = "Scored neutrally — automated assessment was unavailable."

        # Any overflow beyond the per-call cap (rare) also gets the neutral fallback.
        for p in candidates[MAX_CONTACTS_PER_CALL:]:
            p["strategic_score"] = 50
            p["strategic_reasoning"] = "Scored neutrally — beyond the per-company scoring batch."

        candidates.sort(key=lambda c: c["strategic_score"], reverse=True)
        return candidates

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

        prompt = ChatPromptTemplate.from_template(
            """You are a B2B sales strategist deciding WHO to contact at a target account. Produce a
SPREAD of scores that reflects how decisive each person is FOR THIS SPECIFIC deal — never a generic
seniority ranking, and never the same number for everyone.

SENDER'S IDEAL BUYER PROFILES: {target_profiles}
PAINS THE SENDER SOLVES: {pains_solved}
CAMPAIGN OBJECTIVE (the user's goal): {objective}
(The per-company TARGET COMPANY INDUSTRY, RESEARCH, and CONTACTS are provided at the
very END, after these instructions — they are the only inputs that change per company.)

STEP 1 — Define the BUYING COMMITTEE for THIS deal. From the CAMPAIGN OBJECTIVE and the TARGET COMPANY
INDUSTRY (shown below), work out which functions/departments own the budget, the decision, and the internal-champion
role for THIS specific solution in THIS specific industry. The right functions CHANGE with the objective
and the industry — derive them, do not assume a fixed list. Examples (illustrative, not exhaustive):
  • predictive-maintenance / industrial-data offering to a MANUFACTURER -> Operations, Plant,
    Maintenance, Engineering, Production lead the committee; Marketing/HR are irrelevant.
  • marketing-analytics offering to a RETAILER -> Marketing, Growth, E-commerce, Merchandising lead;
    Plant/Maintenance are irrelevant.
  • compliance/security offering to a BANK -> Risk, Compliance, CISO/Security, IT lead.

STEP 2 — Score EVERY contact's role_fit (0-100). The score is a DYNAMIC judgement from THREE inputs, and
it must change with the objective, the industry, AND the individual person:
  (a) FUNCTION match — does their TITLE + DEPARTMENT sit in the committee you derived for THIS
      objective+industry? This is the primary driver.
  (b) SENIORITY — within the right function, more authority (Director/VP/Head/C-level) scores higher than
      a manager or individual contributor; a senior person in an IRRELEVANT function still scores LOW
      (seniority never rescues a wrong function).
  (c) TENURE — within the same function and seniority, longer time-in-role / time-at-company indicates
      deeper authority and influence and scores higher; a brand-new hire scores lower.
Bands (apply after weighing a+b+c):
  • 85-100 : right function AND senior decision-owner for this exact solution (the primary buyer here).
  • 60-84  : right function, strong influence/champion (senior but not the owner, or owner-adjacent).
  • 30-59  : adjacent/influencer function, or junior within the right function — loop in, does not own it.
  • 0-29   : clearly irrelevant function for THIS objective+industry.
SPREAD the scores — do NOT cluster everyone near one number; two people with different functions,
seniority, or tenure should get visibly different scores.

`reasoning` field — STRICT (this is shown to a salesperson in the UI):
  • Write ONE professional, customer-facing sentence about THIS PERSON and the engagement decision —
    speak to their actual title/function and what to do with them (engage them directly as the buyer,
    use as champion/influencer, consider as budget-approver only, look for a better entry point in
    function X instead, etc.).
  • Use NATURAL business language a sales rep would use. NEVER recite the rubric or framework you used.
  • BANNED phrasing (do NOT use these or anything similar — they expose internal logic):
       "irrelevant function", "aligns with the key functions", "does not align with", "matched to
       committee function", "the committee", "buying committee", "key functions of X, Y, or Z",
       department/value quotes like 'Other'/'Marketing'/'Operations' in scare-quotes, "role_fit",
       "scoring rubric", any mention of band names (e.g. "primary buyer band").
  • GOOD examples (cross-industry, illustrative tone — do not copy verbatim):
       "Operations Director — primary buyer for maintenance and uptime decisions; lead with the
        downtime-reduction angle."
       "Managing Director with broad oversight; can sponsor a pilot but typically delegates the
        technical evaluation to the operations or engineering lead."
       "Marketing leadership; not the buyer for this kind of operational tooling — better entry point
        would be the head of operations or plant management."
       "Junior maintenance engineer — useful internal champion to validate technical fit, but not the
        decision-maker."

================= THIS TARGET COMPANY (the only per-company inputs) =================
TARGET COMPANY INDUSTRY: {industry}
TARGET COMPANY RESEARCH: {research_context}

CONTACTS (score EVERY index):
{contacts_block}

Return a ranking entry for EVERY index above."""
        )

        structured = self.llm.with_structured_output(StakeholderRanking)
        chain = prompt | structured
        try:
            result: StakeholderRanking = await chain.ainvoke({
                "target_profiles": str(target_profiles)[:1500],
                "pains_solved": str(pains_solved)[:1500],
                "objective": (objective or "(general outreach)")[:1000],
                "industry": (industry or "(unspecified)")[:300],
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
