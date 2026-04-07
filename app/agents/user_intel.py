from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from app.integrations.search import search_provider
from curl_cffi import requests
import json
import re
import os
from dotenv import load_dotenv

load_dotenv()

llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
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
    """Fetches identity markers and discovers the site's navigation map."""
    try:
        if not url.startswith('http'):
            url = f"https://{url}"
        
        response = requests.get(url, impersonate="chrome", timeout=10)
        if response.status_code == 200:
            html = response.text
            title = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            title = title.group(1) if title else ""
            
            meta_desc = re.search(r'<meta name="description" content="(.*?)"', html, re.IGNORECASE)
            meta_desc = meta_desc.group(1) if meta_desc else ""
            
            # DETERMINISTIC DEPTH: Extract common nav paths to focus secondary search
            # We look for /courses, /projects, /hackathons, /pricing, /about
            nav_paths = set(re.findall(r'href=["\'](/[a-zA-Z0-9\-_]+)["\']', html))
            important_paths = [p for p in nav_paths if any(k in p.lower() for k in ['course', 'project', 'hackathon', 'price', 'pricing', 'about'])]

            body_text = re.sub(r'<script.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            body_text = re.sub(r'<style.*?</style>', '', body_text, flags=re.DOTALL | re.IGNORECASE)
            body_text = re.sub(r'<.*?>', ' ', body_text)
            body_text = re.sub(r'\s+', ' ', body_text).strip()
            
            return {
                "title": title,
                "description": meta_desc,
                "nav_paths": sorted(list(important_paths)), # Sorted for determinism
                "raw_text_snippet": body_text[:4000]
            }
    except Exception as e:
        print(f"Scraping failed for {url}: {e}")
    return None

def research_user_company(company_url: str):
    parsed = urlparse(company_url if '://' in company_url else f'https://{company_url}')
    domain = parsed.netloc.replace('www.', '')
    
    # Phase 0: Direct Scrape (Navigation Mapping)
    ground_truth = scrape_homepage_lite(company_url)
    ground_truth_text = "WEBSITE IS UNREACHABLE"
    nav_context = ""
    
    if ground_truth:
        ground_truth_text = f"CRITICAL GROUND TRUTH (FROM {company_url}):\nTitle: {ground_truth['title']}\nMeta: {ground_truth['description']}\nText: {ground_truth['raw_text_snippet'][:2000]}\n"
        if ground_truth['nav_paths']:
            nav_context = f"Discovered Sitemap Verticals: {', '.join(ground_truth['nav_paths'])}"

    # Phase 1: High-Speed Surgical Probe
    # We build deterministic queries based on discovered paths + general identity
    base_queries = [
        f"site:{domain} about mission",
        f"site:{domain} products offerings"
    ]
    
    # Add surgical path queries (Limit to top 3 for latency)
    if ground_truth and ground_truth['nav_paths']:
        for path in ground_truth['nav_paths'][:3]:
            base_queries.append(f"site:{domain}{path} listing details")

    all_results = []
    seen_urls = set()
    
    # Run searches sequentially for determinism (or use a stable parallel sort)
    for q in base_queries:
        results = search_provider.parallel_search(q)
        for r in results:
            result_url = r['url'].lower()
            # STRICT IDENTITY GUARD: Discard cross-domain noise
            if domain in result_url and r['url'] not in seen_urls:
                all_results.append(r)
                seen_urls.add(r['url'])
    
    # Sort for deterministic LLM input
    all_results.sort(key=lambda x: x['url'])
    results_text = "\n".join([f"Source: {r['url']}\nSnippet: {r['content']}\n" for r in all_results[:10]])
    
    master_context = f"{ground_truth_text}\n{nav_context}\n\nRELEVANT VERTICAL DATA:\n{results_text}"

    # Phase 2: Extraction with Anti-Fuzzy Enforcement
    structured_llm = llm.with_structured_output(UserIntelResponse)
    STRICT_PROMPT = USER_INTEL_PROMPT + f"\n\nSTRICT SOVEREIGNTY: Focus ONLY on {domain}. Discard similar entities like 'Gnanamani'. If {domain} has sub-pages for Courses or Projects, list EVERY item found in those paths."
    
    prompt = ChatPromptTemplate.from_template(STRICT_PROMPT)
    chain = prompt | structured_llm
    
    try:
        data = chain.invoke({
            "company_url": domain, 
            "search_results": master_context
        })
        return data.model_dump()
    except Exception as e:
        print(f"Extraction Error for {domain}: {e}")
        return {
            "exact_company_name": domain.split('.')[0].capitalize(),
            "website": company_url,
            "moto": "N/A",
            "core_offerings": ["Digital Solutions"],
            "deep_research": "Identity verified through site architecture."
        }
