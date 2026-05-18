import asyncio
import os
import json
import logging
import re
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from typing import Dict, List, Optional
from app.db import models

logger = logging.getLogger(__name__)

class EmailVariant(BaseModel):
    subject: str
    body: str

class EmailDraftSet(BaseModel):
    subject: str = Field(description="High-impact, professional subject line")
    body: str = Field(description="The full 3-paragraph business email body")
    strategic_observation: str = Field(description="The core insight used for personalization")
    pain_hypothesis: str = Field(description="The specific business pain being addressed")
    personalization_hook: str = Field(description="The specific news or growth hook used")

class DraftingService:
    def __init__(self):
        self.llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.7, max_tokens=4000) 

    def generate_draft_set(self, db, dm_id: str):
        """
        Stage 6 — Narrative Messaging Agent (V2 Core)
        Contextual synthesis of Sender Context + Target Context.
        Synchronous wrapper for Celery workers.
        """
        return asyncio.run(self.agenerate_draft_set(db, dm_id))

    async def agenerate_draft_set(self, db, dm_id: str):
        # 1. Gather Context Cluster
        dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
        if not dm: return None
        
        target_co = dm.target_company
        campaign = dm.campaign
        user_intel = campaign.user_intel
        
        if not user_intel: return None

        # 2. Extract structured intelligence (Direct Column Access)
        prompt = ChatPromptTemplate.from_template("""
        You are a Strategic Ghostwriter & Business Architect. 
        Your task is to synthesize a high-fidelity business case into a 3-paragraph outreach set.

        SENDER IDENTITY (Our DNA):
        - Company: {sender_name}
        - Services: {sender_services}
        - Capability Map: {sender_map}

        TARGET RECIPIENT:
        - Name: {prospect_name}
        - Title: {prospect_role}
        - Seniority: {prospect_seniority}
        - Company: {target_company}
        
        STRATEGIC INTELLIGENCE (THE BUSINESS CASE):
        - Master Research Dossier: {research_summary}
        - Growth/News Hooks: {growth_hooks}, {news_hooks}
        - Identified Pains: {pain_hooks}
        - Why Now: {opportunity_reason}
        
        NARRATIVE STRATEGY (STRICT 3-PARAGRAPH FORMAT):
        0. GREETING: Open with a professional greeting (e.g., "Hi {prospect_first_name}," or "Hello {prospect_first_name},").
        1. Paragraph 1 (THE HOOK): Lead with a specific insight from the 'Master Research Dossier'. Anchor the email in their current reality (Acquisitions, Expansion, or News).
        2. Paragraph 2 (THE BRIDGE): Connect that reality to a specific mandate relevant to their Title ({prospect_role}). Explain how {sender_name}'s capabilities solve the 'Identified Pains' found in the research.
        3. Paragraph 3 (THE ASK): A professional discovery call request that respects their Seniority ({prospect_seniority}).
        
        TASK:
        Write a single, hyper-personalized, and professional business email to {prospect_first_name}. 
        The email must be exactly 3 paragraphs + a greeting.
        - Paragraph 1: The 'Hook'. Connect their recent news/growth ({news_hooks}) to a unique strategic observation.
        - Paragraph 2: The 'Business Case'. Link your service ({sender_services}) to a specific pain hypothesis derived from the research.
        - Paragraph 3: The 'Direct Ask'. Suggest a low-friction conversation based on their opportunity reason ({opportunity_reason}).

        STRICT CONSTRAINTS:
        - NO placeholders whatsoever. 
        - DO NOT include bracketed signature blocks. 
        - Sign off using only the Brand Name: "{sender_name}".
        - Tone: Senior, direct, insight-driven. Zero marketing fluff.
        - LENGTH: 100-120 words.
        """)
        
        structured_llm = self.llm.with_structured_output(EmailDraftSet)
        chain = prompt | structured_llm
        
        first_name = dm.name.split(' ')[0] if dm.name and ' ' in dm.name else (dm.name or "there")

        try:
            drafts = await chain.ainvoke({
                "sender_name": user_intel.company_name,
                "sender_services": user_intel.offerings,
                "sender_map": json.dumps(user_intel.v2_intel.get("capability_to_pain_map", [])) if user_intel.v2_intel else "[]",
                "prospect_name": dm.name,
                "prospect_first_name": first_name,
                "target_company": target_co.name,
                "prospect_role": dm.position or "Executive",
                "prospect_seniority": dm.seniority or "Management",
                "research_summary": target_co.research_summary or "N/A",
                "growth_hooks": ", ".join(target_co.growth_hooks or []),
                "pain_hooks": ", ".join(target_co.pain_hooks or []),
                "news_hooks": ", ".join(target_co.news_hooks or []),
                "opportunity_reason": target_co.opportunity_reason or ""
            })
            
            # Clean single line breaks to prevent jagged hard-wrapping on UI
            cleaned_body = re.sub(r'(?<!\n)\n(?!\n)', ' ', drafts.body)
            
            # 3. Post-process into a structured response for workers
            final_drafts = {
                "primary": {"subject": drafts.subject, "body": cleaned_body}
            }
            
            # Create a compatibility object that looks like EmailDraftSet to the worker
            class CompatibilityDraftSet:
                def __init__(self, variants, strategic, pain, hook):
                    self.variants = variants
                    self.strategic_observation = strategic
                    self.pain_hypothesis = pain
                    self.personalization_hook = hook
            
            return CompatibilityDraftSet(
                final_drafts, 
                drafts.strategic_observation, 
                drafts.pain_hypothesis, 
                drafts.personalization_hook
            )
        except Exception as e:
            logger.error(f"Drafting error for {target_co.domain}: {e}")
            return None
