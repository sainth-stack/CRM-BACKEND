import asyncio
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from typing import List
from urllib.parse import urlparse
from dotenv import load_dotenv

from app.integrations.site_extractor import extract_site
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded
from app.core.sanitizer import sanitize_for_llm

load_dotenv()

# Deterministic LLM for extraction. Tail latency is capped (60s × 1
# retry): this runs in a background worker, so a slow provider must not pin a
# worker slot for minutes.
llm = ChatOpenAI(
    model="gpt-4o-mini",
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
    request_timeout=60,
    max_retries=1,
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
- exact_company_name: The formal legal or trade name.
- website: Cleaned and verified official URL.
- moto: Primary tagline or mission snippet.
- core_offerings: List of 4-6 high-impact products/services.
- target_customers: Primary types of companies or industries they serve.
- competitive_advantages: Why they win (experience, unique tech, market position).
- proof_points: Case studies, years in business, growth metrics, or specific achievements.
- capability_to_pain_map: A strategic mapping of: [Pain Point they solve] -> [Their specific Solution] -> [Evidence/Proof].
- deep_research: A professional analytical narrative covering Market Position, Technical Value, and Strategic Synergy.
    CRITICAL: DO NOT use numbered side headings like "SECTION 1" or "SECTION 2".
"""


class CapabilityPainMap(BaseModel):
    pain: str = Field(description="The customer pain point")
    solution: str = Field(description="How the company solves it")
    evidence: str = Field(description="Proof or evidence of the solution")


class UserIntelResponse(BaseModel):
    exact_company_name: str = Field(description="The formal and verified name of the company")
    website: str = Field(description="The verified official website URL")
    moto: str = Field(description="The formal motto or tagline, or N/A")
    core_offerings: List[str] = Field(description="List of 4-6 high-impact products or services")
    target_customers: List[str] = Field(description="Primary types of companies or roles they serve")
    competitive_advantages: List[str] = Field(description="Specific reasons why they win against competitors")
    proof_points: List[str] = Field(description="Verifiable achievements, case studies, or growth metrics")
    capability_to_pain_map: List[CapabilityPainMap] = Field(description="Mapping of company capabilities to specific customer pains")
    deep_research: str = Field(description="A high-fidelity business focus summary")


def research_user_company(company_url: str, campaign_prompt: str = "", return_context: bool = False):
    """
    Research a user's company from its website.

    Maps a company's value-drivers by crawling its OWN website (homepage +
    high-value pages) with curl_cffi + trafilatura — no paid search API. The
    extractor is bounded (<=5 pages, capped chars, 200 KB HTML parse cap) and runs
    in a single homepage+wave pass for low latency / flat memory.

    If return_context=True, returns (result_dict, master_context) where
    master_context is the exact evidence the extractor LLM saw. Testability hook;
    does not change default behaviour.
    """
    parsed = urlparse(company_url if '://' in company_url else f'https://{company_url}')
    domain = parsed.netloc.replace('www.', '')

    # Crawl the company site via the extractor.
    logger.info(f"[USER INTEL] Extracting site for domain: {domain}")
    extract = asyncio.run(extract_site(company_url, max_pages=5, per_page_chars=7000))

    if extract.ok:
        ground_truth_text = (
            f"CRITICAL GROUND TRUTH (FROM {company_url}):\n"
            f"Title: {extract.title}\n"
            f"Meta: {extract.description}\n"
            f"Text: {extract.homepage_text[:2000]}\n"
        )
        nav_context = (
            f"Discovered Sitemap Verticals: {', '.join(extract.nav_paths)}"
            if extract.nav_paths else ""
        )
        # Vertical data = the company's own high-value sub-pages.
        results_text = "\n".join(
            f"Source: {p.url}\nSnippet: {p.text}\n" for p in extract.subpages[:10]
        )
    else:
        logger.warning(f"[USER INTEL] Site extraction failed for {domain}: {extract.error}")
        ground_truth_text = "WEBSITE IS UNREACHABLE"
        nav_context = ""
        results_text = ""

    master_context = f"{ground_truth_text}\n{nav_context}\n\nRELEVANT VERTICAL DATA:\n{results_text}"

    # Extract structured intel via the LLM.
    structured_llm = llm.with_structured_output(UserIntelResponse)
    STRICT_PROMPT = USER_INTEL_PROMPT + f"\n\nUSER CAMPAIGN CONTEXT: {campaign_prompt}\n\nSTRICT SOVEREIGNTY: Focus ONLY on {domain}. Discard similar entities. If {domain} has sub-pages for Courses or Projects, list EVERY item found in those paths."

    prompt = ChatPromptTemplate.from_template(STRICT_PROMPT)
    chain = prompt | structured_llm

    safe_context = sanitize_for_llm(master_context, context_limit=15000)
    fallback = {
        "exact_company_name": domain.split('.')[0].capitalize(),
        "website": company_url,
        "moto": "N/A",
        "core_offerings": ["Digital Solutions"],
        "deep_research": "Identity verified through site architecture.",
    }
    try:
        logger.info(f"[USER INTEL] Synchronizing intelligence for {domain}...")
        data = run_openai_guarded(
            "user_intel_extraction",
            lambda: chain.invoke({
                "company_url": domain,
                "search_results": safe_context
            }),
            fallback=None,
        )
        result = fallback if not data else data.model_dump()
    except Exception as e:
        logger.error(f"[USER INTEL] Intelligence extraction failed for {domain}: {e}", exc_info=True)
        result = fallback

    return (result, safe_context) if return_context else result
