import asyncio
import logging

logger = logging.getLogger("Phase1V2")


class UserIntelService:
    """Stage 2 — Brand Intelligence (thin orchestrator).

    The heavy work (lite homepage scrape + targeted search + LLM synthesis) lives
    in app.agents.user_intel. This class only adapts the agent's response into the
    persistence shape the worker writes to UserCompanyIntel.
    """

    async def research_user_company(self, url: str, prompt: str):
        from app.agents.user_intel import research_user_company as agent_research

        logger.info(f"[STAGE 2] Researching sender company: {url}")

        # Heavy agent work runs off the event loop; holds no DB session.
        research_data = await asyncio.to_thread(agent_research, url, prompt)
        if not research_data:
            return None

        return {
            "exact_company_name": research_data.get("exact_company_name"),
            "website": research_data.get("website"),
            "motto": research_data.get("motto"),
            "core_offerings": research_data.get("core_offerings"),
            "deep_research": research_data.get("deep_research"),
            "target_customers": research_data.get("target_customers"),
            "competitive_advantages": research_data.get("competitive_advantages"),
            "proof_points": research_data.get("proof_points"),
            "capability_to_pain_map": research_data.get("capability_to_pain_map"),
            "v2_intel": research_data,  # Full structured response
        }
