import re
import logging
from typing import List, Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

logger = logging.getLogger("StakeholderRanking")

class RelevanceScore(BaseModel):
    score: int = Field(description="Score 0-100 of how relevant this stakeholder is to the sender's mission.")
    reasoning: str = Field(description="1-sentence logic for the score.")

class StakeholderRankingService:
    """
    V3 Strategic Stakeholder Ranking Engine.
    Uses AI Role Matching against Brand DNA to identify high-fidelity targets.
    """
    
    def __init__(self):
        self.llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    async def rank_stakeholders_with_ai(self, prospects: List[Dict[str, Any]], user_intel: dict, research_context: str = "") -> List[Dict[str, Any]]:
        """
        Ranks prospects using AI to match their roles against the Brand Strategist Dossier 
        and the Deep Research findings.
        """
        # Prepare the context from the Brand DNA
        target_profiles = user_intel.get('target_customers', [])
        pains_solved = user_intel.get('capability_to_pain_map', [])
        
        prompt_template = ChatPromptTemplate.from_template("""
        You are a Strategic Sales Architect.
        Identify the relevance of these corporate roles to the sender's solution.
        
        SENDER'S TARGET PROFILES:
        {target_profiles}
        
        PAINS WE SOLVE:
        {pains_solved}
        
        CONTACT TO EVALUATE:
        - Name: {name}
        - Title: {title}
        - Time in Role: {time_in_role}
        - Time at Company: {time_at_company}
        
        DEEP RESEARCH CONTEXT (Target Company):
        {research_context}
        
        TASK:
        Assign a Relevance Score (0-100) based on how likely this person is to be a key stakeholder, decision-maker, or champion for the sender's services. 
        Prioritize roles that align with both the PAINS WE SOLVE and the specific DEEP RESEARCH CONTEXT found for this company.
        """)
        
        structured_llm = self.llm.with_structured_output(RelevanceScore)
        chain = prompt_template | structured_llm
        
        scored_prospects = []
        
        # We only AI-rank the top 10 potential candidates to save latency/cost
        candidates = prospects[:15] 
        
        for p in candidates:
            try:
                res = await chain.ainvoke({
                    "target_profiles": str(target_profiles),
                    "pains_solved": str(pains_solved),
                    "name": p.get('contact_full_name', 'N/A'),
                    "title": p.get('position') or p.get('title') or 'N/A',
                    "time_in_role": p.get('time_in_role', 'N/A'),
                    "time_at_company": p.get('time_at_company', 'N/A'),
                    "research_context": research_context
                })
                
                # Combine AI Relevance Score with reachability
                reachability = 30 if (p.get('primary_email') or p.get('email_1_validation') == 'valid') else 0
                total_score = (res.score * 0.7) + reachability
                
                p['strategic_score'] = total_score
                p['strategic_reasoning'] = res.reasoning
                scored_prospects.append(p)
            except Exception as e:
                logger.error(f"Ranking error for {p.get('contact_full_name')}: {e}")
                p['strategic_score'] = 0
                scored_prospects.append(p)

        # Sort by score descending
        ranked = sorted(scored_prospects, key=lambda x: x['strategic_score'], reverse=True)
        return ranked
