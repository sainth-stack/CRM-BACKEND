import os
import asyncio
from tavily import TavilyClient
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from typing import List
import logging
from app.core.config import settings

logger = logging.getLogger("Phase1V2")

class CapabilityPainMatch(BaseModel):
    pain: str = Field(description="The specific business pain or challenge detected.")
    solution: str = Field(description="How the sender's service solves this specific pain.")
    evidence: str = Field(description="Proof point or case study reference.")

class UserCompanyDossier(BaseModel):
    company_summary: str = Field(description="High-level overview of the sender's company.")
    services: List[str] = Field(description="List of primary services or products.")
    target_customers: List[str] = Field(description="Description of the ideal customer profile.")
    pain_points_solved: List[str] = Field(description="Specific business pains the sender addresses.")
    industries_served: List[str] = Field(description="Industries the sender has experience in.")
    competitive_advantages: List[str] = Field(description="What makes them better than competitors?")
    proof_points: List[str] = Field(description="Stats, awards, or big client names.")
    capability_to_pain_map: List[CapabilityPainMatch] = Field(description="Critical mapping of capabilities to specific target pains.")
    tone_of_voice: str = Field(description="The brand's communication style (e.g. professional, disruptive, empathetic).")

class UserIntelService:
    def __init__(self):
        self.tavily = TavilyClient(api_key=settings.TAVILY_API_KEY)
        self.llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, request_timeout=120, max_retries=2)

    def get_context_from_db(self, db, campaign_id: str):
        """Retrieves and structures user company intelligence for LLM context."""
        from app.db import models
        import json
        
        intel = db.query(models.UserCompanyIntel).filter(models.UserCompanyIntel.campaign_id == campaign_id).first()
        if not intel:
            return "No user company intelligence available."
            
        try: offerings = json.loads(intel.offerings)
        except: offerings = [intel.offerings]
        
        context = f"COMPANY: {intel.company_name}\n"
        context += f"CORE OFFERINGS: {', '.join(offerings)}\n"
        context += f"STRATEGIC RESEARCH:\n{intel.deep_research}\n"
        
        return context

    async def research_user_company(self, url: str, prompt: str):
        """
        Stage 2 — Brand Intelligence Agent (Consolidated V2).
        Delegates research and extraction to the specialized UserIntelAgent.
        """
        from app.agents.user_intel import research_user_company as agent_research
        
        logger.info(f"🧠 [STAGE 2] Mobilizing Brand Intelligence Agent for: {url}")
        
        # Call the high-fidelity agent logic
        # agent_research handles the lite-scrape, search, and LLM synthesis
        research_data = await asyncio.to_thread(agent_research, url, prompt)
        
        if not research_data:
            return None

        return {
            "exact_company_name": research_data.get("exact_company_name"),
            "website": research_data.get("website"),
            "motto": research_data.get("moto"),
            "core_offerings": research_data.get("core_offerings"),
            "deep_research": research_data.get("deep_research"),
            "target_customers": research_data.get("target_customers"),
            "competitive_advantages": research_data.get("competitive_advantages"),
            "proof_points": research_data.get("proof_points"),
            "capability_to_pain_map": research_data.get("capability_to_pain_map"),
            "v2_intel": research_data # Store the full structured response in v2_intel
        }
