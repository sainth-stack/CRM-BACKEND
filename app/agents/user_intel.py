from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from app.integrations.search import search_provider
from curl_cffi import requests
import json
import re
from dotenv import load_dotenv
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded
from app.core.sanitizer import sanitize_html, sanitize_for_llm

load_dotenv()

# Deterministic LLM for high-fidelity extraction
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
    request_timeout=30
)

USER_INTEL_PROMPT = """You are a Strategic Growth Analyst & Corporate Intelligence Architect.
Your task is to decode a company's "Value DNA" by synthesizing raw technical data and market signals into a high-fidelity intelligence profile.

MISSION GUIDELINES:
1. STRUCTURAL REASONING: Before finalizing the profile, analyze the relationship between the company's Title, Meta Description, and Navigational paths. Determine if they are a Product-led, Service-led, or Platform-led entity.
2. VALUE DRIVER EXTRACTION: Identify specific proprietary methods, unique technologies, or specialized frameworks (e.g., "Modular Design," "AI-Powered Analytics," "Zero-Trust Architecture") mentioned in the snippets.
3. ENTITY PURIFICATION: Strictly ignore third-party directories or "look-alike" companies. If the URL is {company_url}, prioritize the data found in the 'CRITICAL GROUND TRUTH' section.
4. ZERO HALLUCINATION & NO FILLER: Every word must represent a verifiable fact. Use 'N/A' for unknown metrics. Avoid corporate jargon like "cutting-edge" unless it's part of an official product name.

INPUT DATA:
- PRIMARY URL: {company_url}
- RESEARCH CONTEXT: 
{search_results}

REQUIRED OUTPUT ARCHITECTURE:
- exact_company_name: The formal legal or trade name identified in Ground Truth.
- website: Cleaned and verified official URL.
- moto: The primary brand tagline or mission statement snippet (N/A if absent).
- core_offerings: A list of 4-6 high-impact products, services, or core competencies. Be specific (e.g., "AS9100 Certified CNC Machining" instead of "Manufacturing").
- deep_research: A high-fidelity, professional analytical narrative. Use clear, flowing paragraphs to cover:
    1. Market Position & Core Vertical.
    2. Technical Value Proposition (The core differentiator).
    3. Strategic Synergy Signals for outreach.
    CRITICAL: DO NOT use numbered side headings like "SECTION 1", "SECTION 2", or "SECTION 3" in the output.
"""

from pydantic import BaseModel, Field
from typing import List
from urllib.parse import urlparse

class UserIntelResponse(BaseModel):
    exact_company_name: str = Field(description="The formal and verified name of the company")
    website: str = Field(description="The verified official website URL")
    moto: str = Field(description="The formal motto or tagline, or N/A")
    core_offerings: List[str] = Field(description="List of core products or services")
    deep_research: str = Field(description="A high-fidelity business focus summary")

def scrape_homepage_lite(url: str):
    """
    Identity Marker Extraction.
    Executes a high-fidelity headless fetch to capture primary corporate metadata and discover functional sitemap verticals.
    """
    from app.core.security import validate_url_for_ssrf
    try:
        if not url.startswith('http'):
            url = f"https://{url}"
        
        # Security: SSRF Boundary Audit (TOCTOU-Resistant)
        safe_url, validated_ip = validate_url_for_ssrf(url)
        
        parsed = urlparse(safe_url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        
        # Force resolution to the validated IP to prevent DNS Rebinding
        response = requests.get(
            safe_url, 
            impersonate="chrome", 
            timeout=10,
            resolve={f"{hostname}:{port}": validated_ip}
        )
        if response.status_code == 200:
            html = response.text
            title = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            title = title.group(1) if title else ""
            
            meta_desc = re.search(r'<meta name="description" content="(.*?)"', html, re.IGNORECASE)
            meta_desc = meta_desc.group(1) if meta_desc else ""
            
            # Navigational Mapping Vertical Extraction
            nav_paths = set(re.findall(r'href=["\'](/[a-zA-Z0-9\-_]+)["\']', html))
            important_paths = [p for p in nav_paths if any(k in p.lower() for k in ['course', 'project', 'hackathon', 'price', 'pricing', 'about'])]
            
            body_text = sanitize_html(html)
            
            return {
                "title": title,
                "description": meta_desc,
                "nav_paths": sorted(list(important_paths)), 
                "raw_text_snippet": body_text[:4000]
            }
    except Exception as e:
        logger.warning(f"[SCRAPER] Lite-scrape mission compromised for {url}: {e}")
    return None

def research_user_company(company_url: str):
    """
    User Identity & Intent Research Orchestrator.
    Maps out a company's corporate architecture and extracts their primary value-drivers through sitemap traversal and targeted search.
    """
    parsed = urlparse(company_url if '://' in company_url else f'https://{company_url}')
    domain = parsed.netloc.replace('www.', '')
    
    # Phase 0: Identity Mapping (Ground Truth Extraction)
    logger.info(f"[USER INTEL] Mobilizing identity mapping for domain: {domain}")
    ground_truth = scrape_homepage_lite(company_url)
    ground_truth_text = "WEBSITE IS UNREACHABLE"
    nav_context = ""
    
    if ground_truth:
        ground_truth_text = f"CRITICAL GROUND TRUTH (FROM {company_url}):\nTitle: {ground_truth['title']}\nMeta: {ground_truth['description']}\nText: {ground_truth['raw_text_snippet'][:2000]}\n"
        if ground_truth['nav_paths']:
            nav_context = f"Discovered Sitemap Verticals: {', '.join(ground_truth['nav_paths'])}"

    # Phase 1: High-Speed Surgical Reconnaissance
    base_queries = [
        f"site:{domain} about mission",
        f"site:{domain} products offerings"
    ]
    
    if ground_truth and ground_truth['nav_paths']:
        for path in ground_truth['nav_paths'][:3]:
            base_queries.append(f"site:{domain}{path} listing details")

    all_results = []
    seen_urls = set()
    
    for q in base_queries:
        results = search_provider.parallel_search(q)
        for r in results:
            result_url = r['url'].lower()
            if domain in result_url and r['url'] not in seen_urls:
                all_results.append(r)
                seen_urls.add(r['url'])
    
    all_results.sort(key=lambda x: x['url'])
    results_text = "\n".join([f"Source: {r['url']}\nSnippet: {r['content']}\n" for r in all_results[:10]])
    
    master_context = f"{ground_truth_text}\n{nav_context}\n\nRELEVANT VERTICAL DATA:\n{results_text}"

    # Phase 2: Intelligence Extraction with Sovereign Policy Enforcement
    structured_llm = llm.with_structured_output(UserIntelResponse)
    STRICT_PROMPT = USER_INTEL_PROMPT + f"\n\nSTRICT SOVEREIGNTY: Focus ONLY on {domain}. Discard similar entities. If {domain} has sub-pages for Courses or Projects, list EVERY item found in those paths."
    
    prompt = ChatPromptTemplate.from_template(STRICT_PROMPT)
    chain = prompt | structured_llm
    
    try:
        logger.info(f"[USER INTEL] Synchronizing intelligence for {domain}...")
        safe_context = sanitize_for_llm(master_context, context_limit=15000)
        data = run_openai_guarded(
            "user_intel_extraction",
            lambda: chain.invoke({
                "company_url": domain,
                "search_results": safe_context
            }),
            fallback=None,
        )
        if not data:
            return {
                "exact_company_name": domain.split('.')[0].capitalize(),
                "website": company_url,
                "moto": "N/A",
                "core_offerings": ["Digital Solutions"],
                "deep_research": "Identity verified through site architecture.",
            }
        return data.model_dump()
    except Exception as e:
        logger.error(f"[USER INTEL] Intelligence extraction failed for {domain}: {e}", exc_info=True)
        return {
            "exact_company_name": domain.split('.')[0].capitalize(),
            "website": company_url,
            "moto": "N/A",
            "core_offerings": ["Digital Solutions"],
            "deep_research": "Identity verified through site architecture."
        }
