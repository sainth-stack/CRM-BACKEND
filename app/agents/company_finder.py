import os
import json
import requests
import re
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# LangChain / OpenAI
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

# Load environment
load_dotenv()

# API Keys
ZENSERP_API_KEY = os.getenv("ZENSERP_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# --- DATA MODELS ---

class WebsiteExtraction(BaseModel):
    official_url: Optional[str] = Field(description="The most credible official corporate homepage URL.")
    reasoning: str = Field(description="Why this URL was selected.")

class CompanyValidation(BaseModel):
    name: str
    company_type: str = Field(description="The specific sub-vertical (e.g., 'Precision Aerospace Manufacturing').")
    employee_count: str = Field(description="Headcount or range (e.g., '501-1,000').")
    is_industry_match: bool = Field(description="True if sector matches.")
    has_demonstrated_requirement: bool = Field(description="CRITICAL: True ONLY if research shows they actually NEED our specific offerings based on their current operations/growth.")
    requirement_justification: str = Field(description="SPECIFIC EVIDENCE from research that justifies why they need our core offerings (e.g., 'Target is expanding their B2B footprint but shows gaps in X').")
    is_primary_operator: bool = Field(description="True ONLY if they perform the core value-creation of the target industry (e.g., if target is 'Software', they build software. If 'Manufacturing', they make things). REJECT resellers/consultants unless the target industry IS consulting.")
    is_offering_synergy: bool = Field(description="True if our solution is an ROI-positive match for their gaps.")
    synergy_score: int = Field(description="0-100 score weighted heavily by the 'Requirement' audit.")
    is_valid_lead: bool = Field(description="Final decision. True only if Synergy > 80%, has_demonstrated_requirement is True, and is_primary_operator is True.")

class DeduplicatedCompany(BaseModel):
    name: str = Field(description="Clean, official company name.")
    linkedin_url: str = Field(description="LinkedIn company profile URL.")
    description: str = Field(description="Short snippet from search.")

class DeduplicationResult(BaseModel):
    companies: List[DeduplicatedCompany]

# --- PIPELINE COMPONENTS ---

class CompanyFinderPipeline:
    def __init__(self):
        self.llm = ChatOpenAI(
            model="gpt-4o-mini", 
            temperature=0,
            seed=42,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0
        )
        try:
            from tavily import TavilyClient
            self.tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None
        except ImportError:
            self.tavily = None

    def get_domain(self, url: str) -> str:
        try:
            if not url or url == "unknown" or "linkedin.com" in url: return "unknown"
            parsed = urlparse(url)
            domain = parsed.netloc or parsed.path.split('/')[0]
            domain = domain[4:] if domain.startswith("www.") else domain
            return domain.lower()
        except: return "unknown"

    def sanitize_landing_page(self, url: str) -> str:
        try:
            if not url or url == "unknown": return "unknown"
            parsed = urlparse(url)
            if not parsed.scheme: url = "https://" + url; parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}"
        except: return url

    def fetch_zenserp_page(self, query: str, page: int, headers: dict) -> list:
        params = {"q": query, "num": 10, "start": page * 10, "engine": "google"}
        try:
            response = requests.get('https://app.zenserp.com/api/v2/search', headers=headers, params=params, timeout=10)
            response.raise_for_status()
            return response.json().get("organic", [])
        except: return []

    def stage_1_recon(self, industry: str, location: str, size: str) -> list:
        query = f'site:linkedin.com/company "{industry}" "{location}"'
        if size: query += f' "{size}"'
        print(f" [Pipeline] Stage 1: Parallel Zenserp Recon for '{query}'")
        all_results = []
        headers = {"apikey": ZENSERP_API_KEY} if ZENSERP_API_KEY else {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(self.fetch_zenserp_page, query, p, headers) for p in range(3)]
            for f in as_completed(futures):
                all_results.extend(f.result())
        return all_results

    def stage_2_dedup(self, raw_results: list) -> List[DeduplicatedCompany]:
        if not raw_results: return []
        print(f" [Pipeline] Stage 2: Deduplicating {len(raw_results)} snippets...")
        structured_llm = self.llm.with_structured_output(DeduplicationResult)
        sys_prompt = """You are a Lead Integrity Auditor. 
1. Use the provided search snippets to identify ALL unique companies. 
2. Merge duplicates and aliases. 
3. STRATEGIC REJECTION: Reject aggregators like Clutch, G2, Indeed, or YellowPages. 
4. IDENTITY LOCK: Even if a result points to a LinkedIn profile, capture that company as a unique entity. Do NOT reject actual companies just because they are hosted on LinkedIn. 
5. Return a clean, unique list of brand identities."""
        try:
            return structured_llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=json.dumps(raw_results, indent=2))]).companies
        except Exception as e:
            print(f" [Pipeline] Dedup Error: {e}")
            return []

    def execute_advanced_search(self, query: str) -> list:
        """Helper to run a deep Tavily search concurrently."""
        try:
            if not self.tavily: return []
            resp = self.tavily.search(query=query, search_depth="advanced", max_results=5)
            return resp.get('results', [])
        except: return []

    def stage_3_research_one(self, company_name: str, location: str) -> dict:
        """Dual-Stream Parallel Research: Identity + Intent."""
        print(f" [Pipeline] Deep Research: {company_name}...")
        
        identity_results = []
        intent_results = []
        site = "unknown"
        
        # NESTED PARALLELIZATION: Execute Identity and Intent searches concurrently
        queries = [
            f'"{company_name}" "{location}" official website or corporate home page',
            f'"{company_name}" "{location}" Recent News Projects Challenges Growth 2024 2025'
        ]
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            identity_future = executor.submit(self.execute_advanced_search, queries[0])
            intent_future = executor.submit(self.execute_advanced_search, queries[1])
            identity_results = identity_future.result()
            intent_results = intent_future.result()

        # Handle Identity Lock (Website Selection)
        if identity_results:
            try:
                identity_auditor = self.llm.with_structured_output(WebsiteExtraction)
                audit_prompt = f"Identify the OFFICIAL corporate homepage for '{company_name}' from these results. Ignore LinkedIn/News."
                selection = identity_auditor.invoke([SystemMessage(content=audit_prompt), HumanMessage(content=json.dumps(identity_results, indent=2))])
                if selection and selection.official_url:
                    site = self.sanitize_landing_page(selection.official_url)
                    print(f" [IDENTITY] Verified: {company_name} -> {site}")
            except: pass

        return {
            "name": company_name, 
            "website": site, 
            "domain": self.get_domain(site), 
            "raw_research": identity_results + intent_results
        }

    def stage_4_validate(self, industry: str, offerings: List[str], res_data: dict) -> CompanyValidation:
        """Strategic Requirement Cross-Validation."""
        structured_llm = self.llm.with_structured_output(CompanyValidation)
        off_str = ", ".join(offerings)
        
        sys_prompt = f"""Strategic Deal Auditor. 
Target Sector: {industry}. 
Our High-Impact Offerings: {off_str}. 

MISSION: 
1. UNIVERSAL PRIMARY OPERATOR AUDIT: Ascertain the core definition of '{industry}'. Only approve companies that are the PRIMARY VALUE-CREATOR (the actual makers/operators/providers) of that specific industry. Reject companies that merely supply, consult, or distribute to that industry, UNLESS the target industry itself is consulting/distribution.
2. REQUIREMENT AUDIT: From research, identify tangible evidence that they NEED our specific offerings (e.g. they are scaling fast but lack automation).
3. STRICT REJECTION: REJECT if they show no clear operational gap or requirement for our specific solutions.
4. PITCH REDUNDANCY: REJECT if they are a pure service/consulting provider of the same services we offer.

FINAL DECISION: Only is_valid_lead if Requirement Justification is strong AND they are a Primary Operator of {industry}."""
        
        context = json.dumps(res_data.get('raw_research', [])[:8], indent=2) # Send both identity & intent data
        msg = f"Company: {res_data['name']}\nWebsite: {res_data['website']}\nOfferings: {off_str}\nResearch:\n{context}"
        
        try:
            return structured_llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=msg)])
        except:
            return CompanyValidation(name=res_data['name'], is_industry_match=False, is_valid_lead=False, has_demonstrated_requirement=False, requirement_justification="Error", synergy_score=0, company_type="Sync Error", employee_count="N/A", is_offering_synergy=False, is_primary_operator=False)

    def stage_3_synthesize(self, name: str, raw_research: List[Dict]) -> str:
        if not raw_research: return "No verifiable artifacts discovered."
        prompt = f"Transform the following research artifacts for {name} into a clean, human-readable prose intelligence report. Focus on their operational nature and potential needs."
        messages = [SystemMessage(content="Professional Intelligence Analyst."), HumanMessage(content=prompt + "\n" + json.dumps(raw_research, indent=2))]
        try:
            return self.llm.invoke(messages).content.strip()
        except: return "Analysis pending."

# --- PROD INTERFACE ---

def find_target_companies(target_criteria: dict, user_offerings: list):
    pipeline = CompanyFinderPipeline()
    industry = target_criteria.get("industry", "Manufacturing")
    location = target_criteria.get("location", "UK")
    size = target_criteria.get("employee_count", "")

    raw_candidates = pipeline.stage_1_recon(industry, location, size)
    if not raw_candidates: return

    unique_companies = pipeline.stage_2_dedup(raw_candidates)
    if not unique_companies: return

    def process_lead(co_meta):
        # Full Phase Parallelization
        res = pipeline.stage_3_research_one(co_meta.name, location)
        val = pipeline.stage_4_validate(industry, user_offerings, res)
        syn = pipeline.stage_3_synthesize(co_meta.name, res['raw_research'])
        
        return {
            "name": res['name'],
            "website": res['website'],
            "domain": res['domain'],
            "linkedin": co_meta.linkedin_url,
            "location": location,
            "company_type": val.company_type,
            "employee_count": val.employee_count,
            "description": co_meta.description,
            "deep_research": syn,
            "similarity_score": val.synergy_score,
            "score_reason": val.requirement_justification,
            "status": "NEW" if val.is_valid_lead else "REJECTED",
            "rejection_reason": val.requirement_justification if not val.is_valid_lead else None
        }

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(process_lead, c): c for c in unique_companies}
        for f in as_completed(futures):
            try: yield f.result()
            except: pass
