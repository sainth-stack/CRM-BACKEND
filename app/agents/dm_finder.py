from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from app.integrations.search import search_provider
from pydantic import BaseModel, Field
from typing import List
import json
import os
import re
from dotenv import load_dotenv

load_dotenv()

# We use gpt-4o-mini for efficient extraction and auditing
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
)

class DecisionMakerSchema(BaseModel):
    name: str = Field(description="Full Name of the stakeholder")
    position: str = Field(description="Exact and current official title")
    linkedin: str = Field(description="Full LinkedIn profile URL")
    seniority_score: int = Field(description="Score based on executive seniority (0-100)")
    score_reason: str = Field(description="Justification for their active seniority status and current role verification")

class DMFinderResponse(BaseModel):
    decision_makers: List[DecisionMakerSchema] = Field(description="List of validated high-level stakeholders")

def clean_linkedin_url(url: str) -> str:
    """Strips tracking parameters from LinkedIn URLs."""
    if not url: return "unknown"
    # Basic regex to keep the core profile path
    match = re.search(r'(https?://[a-z]+\.linkedin\.com/in/[a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else url

def find_decision_makers(company_name: str, location: str):
    """
    Inputs: Registered company name and city/region.
    Logic:
    1. Two-pass search (C-Suite vs leadership) using Tavily Advanced & DDGS with Location Fencing.
    2. LLM-based extraction, high-fidelity audit (Current vs Former), and deduplication.
    3. Yields Top 3 most senior stakeholders.
    """
    print(f" [DM Finder] Starting localized stakeholder research for: {company_name} in {location}")
    
    # 1. Targeted Query Strategy (Strict Identifier)
    # Using 'intitle' on LinkedIn profiles forces the company name to be a core part of the person's identity or headline.
    query = f'site:linkedin.com/in intitle:"{company_name}" "{location}" ("CEO" OR "CTO" OR "Founder" OR "Managing Director" OR "VP" OR "Director")'
    
    all_raw_results = []
    seen_urls = set()
    
    # 2. Parallel Search Execution
    try:
        results = search_provider.parallel_search(query, include_domains=["linkedin.com"])
        for r in results:
            if r['url'] not in seen_urls:
                all_raw_results.append(r)
                seen_urls.add(r['url'])
    except Exception as e:
        print(f" [DM Finder] Search failed for query '{query}': {e}")
            
    if not all_raw_results:
        print(f" [DM Finder] Zero candidates found for {company_name} in {location}")
        return []

    # 3. LLM Audit with 'Default Reject' Protocol
    structured_llm = llm.with_structured_output(DMFinderResponse)
    
    sys_prompt = f"""
    You are a Cynical Executive Auditor. Your mission is to REJECT candidates. 
    You assume every search result is a false positive unless proven otherwise by IRREFUTABLE evidence in the content.
    
    THE 'DEFAULT REJECT' PROTOCOL:
    1. AMBIGUOUS HEADLINES = REJECT: If the headline is just "[Name] - {company_name}" or "[Name] | {company_name}", they are an EMPLOYEE, not an executive. REJECT.
    2. NO SELF-INFERRED TITLES: Only approve if an EXECUTIVE title (CEO, CTO, Founder, VP, Director, Head) is EXPLICITLY written in the snippet. DO NOT hallucinate or "assume" a title.
    3. ASSOCIATION != EMPLOYMENT: REJECT if they merely "Liked", "Shared", or "Commented" on a post.
    4. TITLE MISMATCH: REJECT if the primary headline mentions a DIFFERENT company.
    5. POSITION WEAKNESS: REJECT: Manager, Employee, Student, Talent Acquisition, Consultant, Guest, or Member. 
       - "Manager" is NOT an executive for our purposes. REJECT.
    6. CURRENT STATUS: You MUST see "[Title] at {company_name}". If it doesn't specify CURRENT, REJECT.
    
    SCORING MATRIX:
    - 90-100: TOP LEADERSHIP (CEO/CTO/Founder/MD) with explicit CURRENT headline at "{company_name}".
    - 85-89: HIGH EXECUTIVES (VP/Director/Head) with explicit CURRENT headline at "{company_name}".
    - 0-84: REJECT (Ambiguous titles, employees, managers, and fans).
    
    FINAL SKEPTICISM CHECK: If there is 1% doubt that this person is a high-level decision maker, REJECT.
    
    LIMIT: Return Top 3 unique, verified-current EXECUTIVES only.
    """
    
    # Use a safe slice to avoid context limits
    all_raw_results = all_raw_results if isinstance(all_raw_results, list) else []
    results_to_process = all_raw_results[:20]
    
    messages = [
        SystemMessage(content=sys_prompt),
        HumanMessage(content=f"Company: {company_name}\nLocation: {location}\nSearch Results (JSON):\n{json.dumps(results_to_process, indent=2)}")
    ]
    
    try:
        print(f" [DM Finder] Auditing {len(results_to_process)} snippets for {company_name} ({location})...")
        response = structured_llm.invoke(messages)
        
        final_dms = []
        for dm in response.decision_makers:
            # We enforce the 85 point floor as requested to ensure executive seniority
            if dm.seniority_score >= 85:
                dm_dict = dm.model_dump()
                dm_dict['similarity_score'] = dm.seniority_score
                dm_dict['linkedin'] = clean_linkedin_url(dm.linkedin)
                final_dms.append(dm_dict)
        
        # Ensure Top 3
        top_3 = sorted(final_dms, key=lambda x: x['similarity_score'], reverse=True)[:3]
        print(f" [DM Finder] Success: Verified {len(top_3)} high-level executives for {company_name}.")
        return top_3
        
    except Exception as e:
        print(f" [DM Finder] LLM Verification failed: {e}")
        return []
