import os
import re
import asyncio
from typing import List, Literal
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from tavily import TavilyClient
import logging
from app.core.config import settings
from app.core.llm import get_chat_llm

logger = logging.getLogger("CompanyValidationService")


# --------------------------------------------------------------------------- #
# Structured outputs                                                           #
# --------------------------------------------------------------------------- #
class DimensionVerdict(BaseModel):
    verdict: Literal["pass", "fail", "unknown"] = Field(
        description="pass = clearly satisfies the requirement; fail = clearly contradicts it; unknown = insufficient evidence."
    )
    evidence: str = Field(description="One sentence citing the specific evidence behind the verdict.")


class ICPJudgment(BaseModel):
    industry_match: DimensionVerdict = Field(description="Target company's business vs the campaign's target industries.")
    location_match: DimensionVerdict = Field(description="Target company's HQ/operating region vs the campaign's target locations.")
    size_match: DimensionVerdict = Field(description="Target company's employee size vs the campaign's target size.")
    strategic_fit: DimensionVerdict = Field(
        description="Whether the target plausibly needs THIS sender's offerings and matches the sender's stated ICP + the campaign objective. Evidence MUST name the specific capability->need bridge."
    )
    overall_reasoning: str = Field(description="1-2 sentence synthesis of the decision.")
    confidence: int = Field(description="0-100 confidence given the quality/quantity of evidence available.")


class DeepDossier(BaseModel):
    relevance_score: int
    reasoning: str = Field(description="A 1-sentence summary of the match.")
    business_opportunity_reason: str = Field(description="The 'Why Now' hook.")
    matched_pains: List[str]
    matched_services: List[str]
    growth_hooks: List[str]
    pain_hooks: List[str]
    news_hooks: List[str]
    executive_summary: str = Field(description="A professional 2-3 paragraph summary for the UI.")


class CompanyValidationService:
    """ICP qualification + deep-research swarm.

    Phase 2: the ICP gate is fully generic — it judges each target company against
    the REAL sender dossier (Brand DNA) and the campaign's own requirements
    (industry / location / size / objective prompt). There is no hardcoded sender
    profile or industry rubric.
    """

    def __init__(self):
        self.tavily = TavilyClient(api_key=settings.TAVILY_API_KEY)
        # Quality-critical judgment runs on the reasoning model (gpt-5-mini by default).
        self.reasoning_llm = get_chat_llm("reasoning", timeout=60)

    # ----------------------------------------------------------------------- #
    # Helpers                                                                  #
    # ----------------------------------------------------------------------- #
    @staticmethod
    def _as_text(value, empty="Any") -> str:
        """Render a list/str field for the prompt; '' / None / [] -> 'Any'.

        Comma/semicolon/newline-separated strings (the multi-value input format)
        are normalised to a clean ', '-joined list so the gate sees each value."""
        if value is None:
            return empty
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(items) if items else empty
        text = str(value).strip()
        if not text:
            return empty
        parts = [p.strip() for p in re.split(r"[,;\n]+", text) if p.strip()]
        return ", ".join(parts) if parts else empty

    async def _gather_research(self, name: str, domain: str, csv_desc: str) -> str:
        """Return web+CSV evidence for a company, reusing any globally cached ICP
        research for the domain to keep decisions stable across campaigns."""
        from app.db.database import SessionLocal
        from app.db import models

        # Read cached research, then close the session BEFORE any network work.
        db = SessionLocal()
        try:
            existing = (
                db.query(models.TargetCompany)
                .filter(
                    models.TargetCompany.domain == domain,
                    models.TargetCompany.icp_research_context != None,  # noqa: E711
                )
                .order_by(models.TargetCompany.updated_at.desc())
                .first()
            )
            cached = existing.icp_research_context if existing and existing.icp_research_context else None
        finally:
            db.close()

        if cached and len(cached.strip()) > 100:
            logger.info(f"♻️ [ICP-CACHE] Reusing research for {domain}.")
            return f"CSV DESCRIPTION: {csv_desc or 'N/A'}\n\nRESEARCH DATA: {cached}"

        query = (
            f"{name} {domain} what they do industry sector headquarters location "
            f"company size employees products services"
        )
        try:
            search_result = await asyncio.wait_for(
                asyncio.to_thread(self.tavily.search, query=query, search_depth="basic"),
                timeout=15,
            )
            research_context = "\n".join([r["content"] for r in search_result.get("results", [])])
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"⏱️ [ICP-RESEARCH] Tavily failed/timed out for {domain}: {e}")
            research_context = ""
        return f"CSV DESCRIPTION: {csv_desc or 'N/A'}\n\nRESEARCH DATA: {research_context}"

    # ----------------------------------------------------------------------- #
    # ICP gate                                                                 #
    # ----------------------------------------------------------------------- #
    async def validate_company(self, company_data: dict, user_intel: dict, campaign_metadata: dict = None):
        """Generic ICP qualification against the real sender DNA + campaign requirements."""
        campaign_metadata = campaign_metadata or {}
        domain = company_data.get("domain") or company_data.get("website")
        name = company_data.get("name")

        csv_loc = company_data.get("location") or "N/A"
        csv_size = company_data.get("size") or "N/A"
        csv_industry = company_data.get("industry") or "N/A"
        csv_desc = company_data.get("description") or ""

        # Campaign requirements (the user's filters for THIS campaign).
        target_industry = self._as_text(campaign_metadata.get("target_industry"))
        target_loc = self._as_text(campaign_metadata.get("target_location"))
        target_size = self._as_text(campaign_metadata.get("target_employee_count"))
        campaign_prompt = self._as_text(campaign_metadata.get("prompt"), empty="(no specific objective provided)")

        # Sender Brand DNA (built dynamically — NO hardcoded profile).
        sender_name = user_intel.get("company_name") or user_intel.get("name") or "the sender"
        sender_offerings = self._as_text(user_intel.get("services") or user_intel.get("offerings") or user_intel.get("core_offerings"))
        sender_customers = self._as_text(user_intel.get("target_customers"))
        sender_advantages = self._as_text(user_intel.get("competitive_advantages"))
        sender_capmap = str(user_intel.get("capability_to_pain_map") or [])[:2000]
        sender_research = self._as_text(user_intel.get("deep_research"), empty="N/A")

        context = await self._gather_research(name, domain, csv_desc)

        prompt = ChatPromptTemplate.from_template(
            """You are an elite B2B ICP (Ideal Customer Profile) qualification analyst.
Decide whether the TARGET COMPANY is a good-fit account for the SENDER to pursue in THIS campaign.

Judge ONLY from the evidence below. Do not invent facts or use outside knowledge.
Where evidence for a dimension is missing, return "unknown" — do NOT guess.

SENDER PROFILE (who is doing the outreach):
- Company: {sender_name}
- Offerings: {sender_offerings}
- Who they sell to (their ICP): {sender_customers}
- Differentiators: {sender_advantages}
- Capability -> customer-pain map: {sender_capmap}
- Business summary: {sender_research}

CAMPAIGN REQUIREMENTS (the user's filters — may contain one or more values each):
- Target industries: {target_industry}
- Target locations: {target_loc}
- Target employee size: {target_size}
- Campaign objective (the user's own words — the company should help satisfy this): {campaign_prompt}

TARGET COMPANY EVIDENCE:
- Name: {name}
- CSV industry: {csv_industry}
- CSV location: {csv_loc}
- CSV size: {csv_size}
- CSV description: {csv_desc}
- Web research: {context}

For each dimension return verdict (pass | fail | unknown) + one-sentence evidence:
1. industry_match: does the target clearly belong to (or is closely adjacent to) ANY of the target industries? If target industries is "Any", return pass.
2. location_match: is the target's HQ/operating region within ANY target location? If "Any", return pass.
3. size_match: does the target's employee size fall within ANY target size band? If "Any", return pass.
4. strategic_fit: does the target plausibly NEED {sender_name}'s offerings, match the sender's stated ICP / capability->pain map, AND serve the campaign objective? Return pass ONLY with a concrete bridge (name the sender capability and the target need it addresses); fail if the sender's solution clearly has no applicability; unknown if signal is too thin.

Then give overall_reasoning (1-2 sentences) and confidence (0-100)."""
        )

        structured_llm = self.reasoning_llm.with_structured_output(ICPJudgment)
        chain = prompt | structured_llm

        try:
            res: ICPJudgment = await chain.ainvoke({
                "sender_name": sender_name,
                "sender_offerings": sender_offerings,
                "sender_customers": sender_customers,
                "sender_advantages": sender_advantages,
                "sender_capmap": sender_capmap,
                "sender_research": (sender_research or "N/A")[:3000],
                "target_industry": target_industry,
                "target_loc": target_loc,
                "target_size": target_size,
                "campaign_prompt": campaign_prompt[:1500],
                "name": name,
                "csv_industry": csv_industry,
                "csv_loc": csv_loc,
                "csv_size": csv_size,
                "csv_desc": (csv_desc or "")[:1500],
                "context": context[:5000],
            })
        except Exception as e:
            logger.error(f"[ICP] Judgment failed for {domain}: {e}")
            return {
                "status": "REJECTED",
                "relevance_score": 0,
                "reasoning": f"ICP evaluation error: {e}",
                "icp_context": context,
            }

        return self._decide(res, context)

    @staticmethod
    def _decide(res: ICPJudgment, context: str) -> dict:
        """Programmatic, hallucination-free decision + score from the verdicts.

        Accept rule: a genuine strategic fit AND no hard filter explicitly fails.
        Missing data ("unknown") never rejects on its own — it lowers confidence
        and score instead, so good leads aren't lost to sparse CSV/web data.
        """
        def pts(v: DimensionVerdict, p_pass: int, p_unknown: int) -> int:
            return p_pass if v.verdict == "pass" else (p_unknown if v.verdict == "unknown" else 0)

        score = (
            pts(res.industry_match, 25, 10)
            + pts(res.location_match, 15, 8)
            + pts(res.size_match, 15, 8)
            + pts(res.strategic_fit, 45, 15)
        )
        score = min(score, 100)

        hard_fail = (
            res.industry_match.verdict == "fail"
            or res.location_match.verdict == "fail"
            or res.size_match.verdict == "fail"
        )
        is_accepted = (res.strategic_fit.verdict == "pass") and not hard_fail
        if not is_accepted:
            score = min(score, 45)

        reasoning = (
            f"{res.overall_reasoning} "
            f"[industry={res.industry_match.verdict}, location={res.location_match.verdict}, "
            f"size={res.size_match.verdict}, strategic_fit={res.strategic_fit.verdict}, "
            f"confidence={res.confidence}] Bridge: {res.strategic_fit.evidence}"
        )

        return {
            "status": "ACCEPTED" if is_accepted else "REJECTED",
            "relevance_score": score,
            "reasoning": reasoning[:800],
            "icp_context": context,
        }

    # ----------------------------------------------------------------------- #
    # Deep research swarm (Stage 4) — unchanged logic, reasoning model         #
    # ----------------------------------------------------------------------- #
    async def deep_research_swarm(self, domain: str, name: str, user_intel: dict, existing_description: str = ""):
        """Tier 3 — Deep Intelligence: advanced wide-angle research in one shot."""
        query = f'"{name}" {domain} company growth expansion layoffs financial news pain points 2024'
        try:
            search_result = await asyncio.wait_for(
                asyncio.to_thread(self.tavily.search, query=query, search_depth="advanced", max_results=15),
                timeout=25,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"⏱️ [DEEP-RESEARCH] Tavily search failed/timed out for {domain}: {e}")
            search_result = {"results": []}

        context = "\n".join(
            [f"Source: {r['url']}\nContent: {r['content'][:1500]}" for r in search_result.get("results", [])]
        )

        prompt_template = ChatPromptTemplate.from_template(
            """You are a Senior Investment Analyst and Outreach Strategist.
        Analyze the research data for {name} and extract high-fidelity outreach hooks.

        RESEARCH DATA (Target Company):
        - Existing Intel: {existing_description}
        - New Web Intel: {context}

        SENDER BRAIN DNA:
        - Capability-to-Pain Map: {capability_map}
        - Proof Points: {proof_points}
        - Core Advantages: {advantages}

        TASK:
        1. Identify specific matched pains.
        2. Assign a relevance score (0-100).
        3. Explain the business opportunity 'Why Now' (Anchor in Growth or News).
        4. List specific growth hooks, pain hooks, and news hooks.
        5. Write a professional 'Executive Summary' (2-3 paragraphs) that synthesizes all the above for a UI dashboard.
        """
        )

        structured_llm = self.reasoning_llm.with_structured_output(DeepDossier)
        chain = prompt_template | structured_llm

        dossier = await chain.ainvoke({
            "name": name,
            "existing_description": existing_description or "N/A",
            "context": context[:8000],
            "capability_map": str(user_intel.get("capability_to_pain_map", []))[:2000],
            "proof_points": str(user_intel.get("proof_points", []))[:1000],
            "advantages": str(user_intel.get("competitive_advantages", []))[:1000],
        })

        return dossier.model_dump(), search_result.get("results", [])
