import os
from tavily import TavilyClient
from ddgs import DDGS
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import warnings
from app.core.logging_config import logger

# Suppress impersonation warnings from ddgs/curl-cffi
warnings.filterwarnings("ignore", category=RuntimeWarning, message="Impersonate.*")
warnings.filterwarnings("ignore", category=UserWarning, message="Impersonate.*")

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

from app.core.retry_utils import with_retries

class SearchProvider:
    """
    Unified Search Intelligence Bridge.
    Coordinates between Tavily (Deep Web) and DuckDuckGo (Agile Fallback) to provide comprehensive context for outreach.
    """
    def __init__(self):
        self.tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

    @with_retries(max_attempts=3, base_delay=2.0)
    def search_tavily(self, query: str, include_domains: list = None):
        """
        Executes a high-fidelity deep research query via Tavily.
        Returns extracted content and raw analysis for LLM consumption.
        """
        if not self.tavily:
            return []
        try:
            kwargs = {"query": query, "search_depth": "advanced", "max_results": 10, "include_raw_content": True}
            if include_domains:
                kwargs["include_domains"] = include_domains
            response = self.tavily.search(**kwargs)
            results = response.get('results', [])
            return [{"title": r.get('title', ''), "url": r.get('url', ''), "content": r.get('content', ''), "raw_content": r.get('raw_content', ''), "score": r.get('score', 0.8)} for r in results]
        except Exception as e:
            logger.error(f"Tavily deep research failure: {e}")
            raise e

    def search_ddgs(self, query: str):
        """
        Executes an agile search via DuckDuckGo with exponential backoff.
        Serves as the primary redundancy layer for the discovery engine.
        """
        import time
        max_retries = 3
        base_delay = 2
        for attempt in range(max_retries):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=15))
                return [{"title": r['title'], "url": r['href'], "content": r['body'], "score": 0.5} for r in results]
            except Exception as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"DuckDuckGo rate limited. Retrying in {delay}s (Attempt {attempt + 1}/{max_retries})...")
                    time.sleep(delay)
                    continue
                logger.error(f"DuckDuckGo fallback search failure: {e}")
                break
        return []

    def parallel_search(self, query: str, include_domains: list = None):
        """
        High-Performance Multi-threaded Search.
        Aggregates results from both primary and secondary providers simultaneously for maximum speed.
        """
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_tavily = executor.submit(self.search_tavily, query, include_domains)
            future_ddgs = executor.submit(self.search_ddgs, query)
            
            try:
                tavily_results = future_tavily.result() or []
            except Exception as e:
                logger.error(f"[SEARCH] Tavily failed completely after retries: {e}")
                tavily_results = []
                
            try:
                ddgs_results = future_ddgs.result() or []
            except Exception as e:
                logger.error(f"[SEARCH] DuckDuckGo failed completely: {e}")
                ddgs_results = []
            
            return tavily_results + ddgs_results

search_provider = SearchProvider()
