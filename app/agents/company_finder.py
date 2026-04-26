import os
import json
import requests
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded

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
    """
    Handles company discovery, deduplication, and AI-driven validation
    for identifying high-quality leads in specific industries.
    """
    def __init__(self):
        self.llm = ChatOpenAI(
            model="gpt-4o-mini", 
            temperature=0,
            seed=42,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
            request_timeout=25
        )
        try:
            from tavily import TavilyClient
            self.tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None
        except ImportError:
            self.tavily = None
            logger.debug("[PIPELINE] Tavily client initialization bypassed.")

    def get_domain(self, url: str) -> str:
        """Extracts the normalized corporate domain from a URL for identity tracking."""
        try:
            if not url or url == "unknown" or "linkedin.com" in url: return "unknown"
            parsed = urlparse(url)
            domain = parsed.netloc or parsed.path.split('/')[0]
            domain = domain[4:] if domain.startswith("www.") else domain
            return domain.lower()
        except Exception: return "unknown"

    def sanitize_landing_page(self, url: str) -> str:
        """Normalizes a raw landing page URL to its primary corporate root."""
        try:
            if not url or url == "unknown": return "unknown"
            parsed = urlparse(url)
            if not parsed.scheme: url = "https://" + url; parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}"
        except Exception: return url

    def fetch_zenserp_page(self, query: str, page: int, headers: dict) -> list:
        """Executes a paginated SERP request via the Zenserp API gateway."""
        params = {"q": query, "num": 10, "start": page * 10, "engine": "google"}
        try:
            response = requests.get('https://app.zenserp.com/api/v2/search', headers=headers, params=params, timeout=10)
            response.raise_for_status()
            return response.json().get("organic", [])
        except Exception as e:
            logger.debug(f"[ZENSERP] Page {page} fetch error: {e}")
            return []

    def stage_1_recon(self, industry: str, location: str, size: str, start_page: int = 0) -> list:
        """Phase 1: Parallel LinkedIn search for target companies."""
        query = f'site:linkedin.com/company "{industry}" "{location}"'
        if size: query += f' "{size}"'
        
        # Determine target range to avoid redundant credit spend
        end_page = 3
        if start_page >= end_page:
            logger.info(f"[PIPELINE] Idempotency Hit: All {end_page} pages already processed. Skipping Recon.")
            return []
            
        logger.info(f"[PIPELINE] Stage 1: Parallel Zenserp Recon for query sector '{query}' (Pages {start_page} to {end_page-1})")
        all_results = []
        headers = {"apikey": ZENSERP_API_KEY} if ZENSERP_API_KEY else {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(self.fetch_zenserp_page, query, p, headers) for p in range(start_page, end_page)]
            for f in as_completed(futures):
                all_results.extend(f.result())
        return all_results

    def stage_2_dedup(self, raw_results: list) -> List[DeduplicatedCompany]:
        """Phase 2: Uses AI to deduplicate results and filter aggregators."""
        if not raw_results: return []
        logger.info(f"[PIPELINE] Stage 2: Deduplicating {len(raw_results)} snippets into unique identities...")
        structured_llm = self.llm.with_structured_output(DeduplicationResult)
        sys_prompt = """You are a Lead Integrity Auditor. 
1. Use the provided search snippets to identify ALL unique companies. 
2. Merge duplicates and aliases. 
3. STRATEGIC REJECTION: Reject aggregators like Clutch, G2, Indeed, or YellowPages. 
4. IDENTITY LOCK: Even if a result points to a LinkedIn profile, capture that company as a unique entity. Do NOT reject actual companies just because they are hosted on LinkedIn. 
5. Return a clean, unique list of brand identities."""
        try:
            result = run_openai_guarded(
                "company_deduplication",
                lambda: structured_llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=json.dumps(raw_results, indent=2))]),
                fallback=DeduplicationResult(companies=[]),
            )
            return result.companies
        except Exception as e:
            logger.error(f"[PIPELINE] Deduplication audit critical failure: {e}")
            return []

    def execute_advanced_search(self, query: str) -> list:
        """Executes a high-fidelity deep research query via the Tavily cluster."""
        try:
            if not self.tavily: return []
            resp = self.tavily.search(query=query, search_depth="advanced", max_results=5)
            return resp.get('results', [])
        except Exception as e:
            logger.debug(f"[TAVILY] Supplemental search error: {e}")
            return []

    def stage_3_research_one(self, company_name: str, location: str) -> dict:
        """
        Phase 3: Deep Identity & Intent Research.
        Mobilizes dual-stream parallel searches to establish corporate identity and operational intent.
        """
        logger.info(f"[PIPELINE] Deep Research: Establishing intelligence for {company_name}...")
        
        identity_results = []
        intent_results = []
        site = "unknown"
        
        # Concurrent execution of identity and intent intelligence streams
        queries = [
            f'"{company_name}" "{location}" official website or corporate home page',
            f'"{company_name}" "{location}" Recent News Projects Challenges Growth 2024 2025'
        ]
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            identity_future = executor.submit(self.execute_advanced_search, queries[0])
            intent_future = executor.submit(self.execute_advanced_search, queries[1])
            identity_results = identity_future.result()
            intent_results = intent_future.result()

        # Phase 3.1: Identity Lock (Official Domain Selection)
        if identity_results:
            try:
                identity_auditor = self.llm.with_structured_output(WebsiteExtraction)
                audit_prompt = f"Identify the OFFICIAL corporate homepage for '{company_name}' from these results. Ignore LinkedIn/News."
                selection = run_openai_guarded(
                    "company_identity_selection",
                    lambda: identity_auditor.invoke([SystemMessage(content=audit_prompt), HumanMessage(content=json.dumps(identity_results, indent=2))]),
                    fallback=WebsiteExtraction(official_url=None, reasoning="Fallback due to temporarily unavailable identity auditor"),
                )
                if selection and selection.official_url:
                    site = self.sanitize_landing_page(selection.official_url)
                    logger.info(f"[IDENTITY] Verified: {company_name} -> {site}")
            except Exception as e:
                logger.debug(f"[IDENTITY] Domain selection error for {company_name}: {e}")

        return {
            "name": company_name, 
            "website": site, 
            "domain": self.get_domain(site), 
            "raw_research": identity_results + intent_results
        }

    def stage_4_validate(self, industry: str, offerings: List[str], res_data: dict) -> CompanyValidation:
        """
        Phase 4: Strategic Cross-Validation.
        Audits the company against target sector definitions and user offerings to verify ROI-positive synergy.
        """
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
        
        context = json.dumps(res_data.get('raw_research', [])[:8], indent=2)
        msg = f"Company: {res_data['name']}\nWebsite: {res_data['website']}\nOfferings: {off_str}\nResearch:\n{context}"
        
        try:
            return run_openai_guarded(
                "company_validation",
                lambda: structured_llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=msg)]),
                fallback=CompanyValidation(
                    name=res_data['name'],
                    is_industry_match=False,
                    is_valid_lead=False,
                    has_demonstrated_requirement=False,
                    requirement_justification="Fallback due to temporarily unavailable validation service",
                    synergy_score=0,
                    company_type="Validation Unavailable",
                    employee_count="N/A",
                    is_offering_synergy=False,
                    is_primary_operator=False,
                ),
            )
        except Exception as e:
            logger.error(f"[PIPELINE] Validation failure for {res_data['name']}: {e}")
            return CompanyValidation(name=res_data['name'], is_industry_match=False, is_valid_lead=False, has_demonstrated_requirement=False, requirement_justification="Sync Error", synergy_score=0, company_type="Sync Error", employee_count="N/A", is_offering_synergy=False, is_primary_operator=False)

    def stage_3_synthesize(self, name: str, raw_research: List[Dict]) -> str:
        """
        Phase 5: Intelligence Synthesis.
        Transforms raw research artifacts into a cohesive, human-readable prose intelligence report.
        """
        if not raw_research: return "No verifiable artifacts discovered."
        prompt = f"Transform the following research artifacts for {name} into a clean, human-readable prose intelligence report. Focus on their operational nature and potential needs."
        messages = [SystemMessage(content="Professional Intelligence Analyst."), HumanMessage(content=prompt + "\n" + json.dumps(raw_research, indent=2))]
        try:
            response = run_openai_guarded(
                "company_research_synthesis",
                lambda: self.llm.invoke(messages),
                fallback=None,
            )
            return response.content.strip() if response else "Analysis pending."
        except Exception as e:
            logger.error(f"[PIPELINE] Intelligence synthesis failure: {e}")
            return "Analysis pending."

# --- PROD INTERFACE ---

def find_target_companies(target_criteria: dict, user_offerings: list, start_page: int = 0):
    """
    Main Orchestrator: Target Company Identification Agent.
    Executes a multi-stage discovery pipeline to identify, research, and validate high-quality leads.
    Yields validated candidates as they are processed.
    """
    pipeline = CompanyFinderPipeline()
    industry = target_criteria.get("industry", "Manufacturing")
    location = target_criteria.get("location", "UK")
    size = target_criteria.get("employee_count", "")
    
    raw_candidates = pipeline.stage_1_recon(industry, location, size, start_page=start_page)
    if not raw_candidates: return

    unique_companies = pipeline.stage_2_dedup(raw_candidates)
    if not unique_companies: return

    def process_lead(co_meta):
        # Parallel Execution Cluster for Speed
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
            except Exception as e:
                logger.error(f"[AGENT] Async lead processing error: {e}")
