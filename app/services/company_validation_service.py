import os
import asyncio
from typing import List, Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from tavily import TavilyClient
import logging
from app.core.config import settings

logger = logging.getLogger("CompanyValidationService")

class CompanyValidationResult(BaseModel):
    industry_match: bool = Field(description="True if target's industry aligns with or is adjacent to requirements (or if requirement is Any/None/empty).")
    location_match: bool = Field(description="True if target's location is compatible with requirements (or if requirement is Any/None/empty).")
    size_match: bool = Field(description="True if target's size is compatible with employee count range (or if requirement is Any/None/empty).")
    strategic_fit: bool = Field(description="True if there is a clear strategic fit with the sender's Brand DNA capabilities.")
    reasoning: str = Field(description="Brief explanation of the decision, detailing structural matches and strategic alignment findings.")

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
    def __init__(self):
        self.tavily = TavilyClient(api_key=settings.TAVILY_API_KEY)
        # Force deterministic behavior: temperature 0 and fixed seed.
        # Explicit timeout + bounded retries so a hung/slow OpenAI socket cannot
        # pin a concurrency slot until the 1-hour task_time_limit.
        self.llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            seed=42,
            timeout=60,
            max_retries=2,
        )

    async def validate_company_first_pass(self, domain: str, name: str, user_intel: dict, csv_description: str = None):
        """Tier 1 — Cheap Qualification: Filter weak accounts early using full Brand DNA."""
        # MANDATORY SURFACE RESEARCH for high-fidelity strategic matching
        query = f"{name} {domain} business model industry B2B B2C summary"
        search_result = await asyncio.to_thread(self.tavily.search, query=query, search_depth="basic")
        research_context = "\n".join([r['content'] for r in search_result['results']])
        
        # Combine CSV data with Research for the ultimate filter
        context = f"CSV DESCRIPTION: {csv_description or 'N/A'}\n\nWEB RESEARCH: {research_context}"
        logger.info(f"🔍 [MANDATORY-RESEARCH] Strategic match data gathered for {name}.")

        prompt_template = ChatPromptTemplate.from_template("""
        DETERMINISTIC LEAD FIT GATEKEEPER

        You are a strict, deterministic lead qualification evaluator.

        Your job is to decide whether the TARGET COMPANY is a high-fidelity match for the SENDER'S profile.

        You must always follow the rules below exactly.
        For the same input, you must always return the same output.
        Do not use assumptions, creativity, external knowledge, or probabilistic reasoning.
        Use only the information provided in the input.

        SENDER IDENTITY:
        - Services: {services}
        - Target Customers: {target_customers}
        - Competitive Advantages: {advantages}

        TARGET COMPANY DATA:
        {target_context}

        SENDER STRATEGIC PROFILE:
        The sender provides AI-powered decision making and predictive maintenance solutions.

        ACCEPT RULE:
        Accept the company if the TARGET COMPANY clearly matches at least one of the following categories:

        1. Manufactures hardware
        2. Operates industrial machinery
        3. Provides workplace safety equipment
        4. Builds robotics
        5. Belongs to an industrial, manufacturing, engineering, equipment, automation, machinery, robotics, hardware, or safety-related business

        Reason:
        These companies are high-fidelity because they likely have operational data, equipment, machines, physical systems, or industrial workflows that can benefit from the sender's AI-powered decision making and predictive maintenance solutions.

        REJECT RULE:
        Reject the company only if one or more of the following are clearly true from the provided input:

        1. The company is purely B2C or consumer retail
        Example: clothing store, fashion retailer, grocery shop, beauty store, consumer-only ecommerce brand

        2. The company belongs to a completely unrelated service sector
        Example: law firm, real estate agency, accounting firm, HR agency, marketing agency, restaurant, travel agency

        3. The company is geographically out of scope, but only if a geographic scope is explicitly specified in the input

        4. The provided TARGET COMPANY DATA is too empty, vague, or insufficient to determine any industrial, hardware, machinery, safety, robotics, manufacturing, engineering, or automation relevance

        DECISION PRIORITY:
        Follow this exact order:

        Step 1:
        Check geographic scope.
        If a geographic scope is explicitly specified and the company is clearly outside that scope, return REJECT.

        Step 2:
        Check if the company clearly belongs to any ACCEPT category.
        If yes, return ACCEPT.

        Step 3:
        Check if the company clearly belongs to any REJECT category.
        If yes, return REJECT.

        Step 4:
        If the information is insufficient or unclear, return REJECT.

        DETERMINISM RULES:
        - Do not infer missing facts.
        - Do not guess the company's industry.
        - Do not use external knowledge.
        - Do not change the decision based on tone, wording style, or formatting.
        - Do not return MAYBE, UNKNOWN, PARTIAL, or any other label.
        - Return only ACCEPT or REJECT as the decision.
        - Reasoning must be exactly one sentence.
        - The reasoning must mention the main rule that caused the decision.

        OUTPUT FORMAT:
        DECISION: ACCEPT or REJECT
        REASONING: One sentence explaining the strategic bridge or rejection reason.
        """)
        
        chain = prompt_template | self.llm
        res = await chain.ainvoke({
            "services": user_intel.get('services', []),
            "target_customers": user_intel.get('target_customers', []),
            "advantages": user_intel.get('competitive_advantages', []),
            "target_context": context
        })
        
        is_accepted = "ACCEPT" in res.content.upper()
        return {
            "decision": "ACCEPTED" if is_accepted else "REJECTED",
            "reasoning": res.content[:500],
            "relevance_score": 70 if is_accepted else 20,
            "needs_deep_research": is_accepted,
            "used_search": used_search
        }

    async def deep_research_swarm(self, domain: str, name: str, user_intel: dict, existing_description: str = ""):
        """Tier 3 — Deep Intelligence: Advanced wide-angle research in one shot."""
        query = f'"{name}" {domain} company growth expansion layoffs financial news pain points 2024'
        try:
            search_result = await asyncio.wait_for(
                asyncio.to_thread(self.tavily.search, query=query, search_depth="advanced", max_results=15),
                timeout=25,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning(f"⏱️ [DEEP-RESEARCH] Tavily search failed/timed out for {domain}: {e}")
            search_result = {"results": []}

        # Trim each source body at the source. The LLM context is capped at 8000
        # chars below, so full page bodies only inflate in-flight memory without
        # adding signal — especially harmful when many companies run concurrently.
        context = "\n".join([f"Source: {r['url']}\nContent: {r['content'][:1500]}" for r in search_result.get('results', [])])

        prompt_template = ChatPromptTemplate.from_template("""
        You are a Senior Investment Analyst and Outreach Strategist.
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
        """)
        
        structured_llm = self.llm.with_structured_output(DeepDossier)
        chain = prompt_template | structured_llm
        
        dossier = await chain.ainvoke({
            "name": name,
            "existing_description": existing_description or "N/A",
            "context": context[:8000],
            "capability_map": str(user_intel.get('capability_to_pain_map', []))[:2000],
            "proof_points": str(user_intel.get('proof_points', []))[:1000],
            "advantages": str(user_intel.get('competitive_advantages', []))[:1000]
        })

        return dossier.model_dump(), search_result.get('results', [])

    async def validate_company(self, company_data: dict, user_intel: dict, campaign_metadata: dict = None):
        """Unified Funnel: T1 (Structural & Strategic LLM Decision)."""
        domain = company_data.get('domain') or company_data.get('website')
        name = company_data.get('name')
        
        # 1. Structural Context
        csv_loc = company_data.get('location', 'N/A')
        csv_size = company_data.get('size', 'N/A')
        csv_industry = company_data.get('industry', 'N/A')
        csv_desc = company_data.get('description', '')

        # 2. Target Context
        target_loc = campaign_metadata.get('target_location', 'Any') if campaign_metadata else 'Any'
        target_size = campaign_metadata.get('target_employee_count', 'Any') if campaign_metadata else 'Any'
        target_industry = campaign_metadata.get('target_industry', 'Any') if campaign_metadata else 'Any'

        # 3. Decision Logic: Combine Structural and Strategic Fit into LLM Gate
        # DETERMINISTIC GLOBAL CACHE: Check if we have research for this domain in ANY campaign
        # This prevents Search Engine Variance from flipping decisions
        from app.db.database import SessionLocal
        from app.db import models
        # Read the cached research value, then CLOSE the session BEFORE any network
        # work — never hold a Neon connection open across the Tavily/LLM call.
        db = SessionLocal()
        try:
            existing_research = db.query(models.TargetCompany).filter(
                models.TargetCompany.domain == domain,
                models.TargetCompany.icp_research_context != None
            ).order_by(models.TargetCompany.updated_at.desc()).first()
            cached_context = (
                existing_research.icp_research_context
                if existing_research and existing_research.icp_research_context
                else None
            )
        finally:
            db.close()

        context = ""
        if cached_context and len(cached_context.strip()) > 100:
            context = f"CSV DESCRIPTION: {csv_desc or 'N/A'}\n\nRESEARCH DATA: {cached_context}"
            logger.info(f"♻️ [GLOBAL-CACHE] Reusing ICP research for {domain} to lock in determinism.")
        else:
            # MANDATORY RESEARCH: Only if we have NO global record.
            # Hard timeout so a hung Tavily socket can't stall a concurrency slot.
            query = f"{name} {domain} business model industry summary"
            try:
                search_result = await asyncio.wait_for(
                    asyncio.to_thread(self.tavily.search, query=query, search_depth="basic"),
                    timeout=15,
                )
                research_context = "\n".join([r['content'] for r in search_result['results']])
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"⏱️ [RESEARCH] Tavily search failed/timed out for {domain}: {e}")
                research_context = ""
            context = f"CSV DESCRIPTION: {csv_desc or 'N/A'}\n\nRESEARCH DATA: {research_context}"
            logger.info(f"🔍 [MANDATORY-RESEARCH] New global ICP research generated for {domain}.")

        prompt_template = ChatPromptTemplate.from_template("""
        DETERMINISTIC LEAD FIT GATEKEEPER
        
        You are a strict, deterministic lead qualification evaluator.
        
        Your job is to classify the TARGET COMPANY against the CAMPAIGN REQUIREMENTS and SENDER BRAND DNA.
        
        You must always follow the rules below exactly.
        For the same input, you must always return the same output.
        Do not use assumptions, creativity, external knowledge, or probabilistic reasoning.
        Use only the information provided in the input.
        
        CAMPAIGN REQUIREMENTS (The Filter):
        - Target Industry: {target_industry}
        - Target Location: {target_loc}
        - Target Size: {target_size}
        
        SENDER BRAND DNA (Strategic Fit):
        - Capabilities: {services}
        - Ideal Customers: {target_customers}
        
        TARGET COMPANY DATA:
        - Name: {name}
        - Location: {csv_loc}
        - Industry: {csv_industry}
        - Size: {csv_size}
        - Research: {context}
        
        SENDER STRATEGIC PROFILE:
        The sender provides AI-powered decision making and predictive maintenance solutions.
        
        CLASSIFICATION RULES:
        
        1. **industry_match** (Boolean):
           - Set to True if target's industry aligns with or is adjacent to '{target_industry}' (or if '{target_industry}' is 'Any', 'None', or empty).
           
        2. **location_match** (Boolean):
           - Set to True if target's location/headquarters aligns with '{target_loc}' (or if '{target_loc}' is 'Any', 'None', or empty).
           
        3. **size_match** (Boolean):
           - Set to True if target's employee count range is compatible with '{target_size}' (or if '{target_size}' is 'Any', 'None', or empty).
           
        4. **strategic_fit** (Boolean):
           Set to True if the TARGET COMPANY clearly matches at least one of the following categories:
           - Manufactures hardware
           - Operates industrial machinery
           - Provides workplace safety equipment
           - Builds robotics
           - Belongs to an industrial, manufacturing, engineering, equipment, automation, machinery, robotics, hardware, or safety-related business
           
           Otherwise, set strategic_fit to False if one or more of the following are clearly true from the provided input:
           - The company is purely B2C or consumer retail (e.g., clothing store, fashion retailer, grocery shop, beauty store, consumer-only ecommerce brand)
           - The company belongs to a completely unrelated service sector (e.g., law firm, real estate agency, accounting firm, HR agency, marketing agency, restaurant, travel agency)
           - The provided TARGET COMPANY DATA is too empty, vague, or insufficient to determine any industrial, hardware, machinery, safety, robotics, manufacturing, engineering, or automation relevance
        
        DETERMINISM RULES:
        - Do not infer missing facts.
        - Do not guess the company's industry.
        - Do not use external knowledge.
        - Reasoning must be exactly one sentence explaining the strategic bridge or rejection reason.
        """)
        
        structured_llm = self.llm.with_structured_output(CompanyValidationResult)
        chain = prompt_template | structured_llm
        
        res = await chain.ainvoke({
            "target_industry": target_industry,
            "target_loc": target_loc,
            "target_size": target_size,
            "services": str(user_intel.get('services', user_intel.get('core_offerings', []))),
            "target_customers": str(user_intel.get('target_customers', [])),
            "name": name,
            "csv_loc": csv_loc,
            "csv_industry": csv_industry,
            "csv_size": csv_size,
            "context": context[:5000]
        })
 
        # Programmatic, Hallucination-Free Score Calculation
        score = 0
        if res.industry_match: score += 30
        if res.strategic_fit: score += 30
        if res.location_match: score += 20
        if res.size_match: score += 20
        
        # Hard Gate Determinism: Any fail in Strategic Fit or Industry Match rejects the lead
        is_accepted = res.strategic_fit and res.industry_match
        
        # Consistent Score Alignment: Enforce score ceiling if rejected to keep UI state consistent
        if not is_accepted:
            score = min(score, 45)
            
        status = "ACCEPTED" if is_accepted else "REJECTED"
        return {
            "status": status,
            "relevance_score": score,
            "reasoning": res.reasoning,
            "icp_context": context
        }
