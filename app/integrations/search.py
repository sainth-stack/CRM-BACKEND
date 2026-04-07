import os
from tavily import TavilyClient
from ddgs import DDGS
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
import warnings

# Suppress impersonation warnings from ddgs/curl-cffi
warnings.filterwarnings("ignore", category=RuntimeWarning, message="Impersonate.*")
warnings.filterwarnings("ignore", category=UserWarning, message="Impersonate.*")

load_dotenv()

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

class SearchProvider:
    def __init__(self):
        self.tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

    def search_tavily(self, query: str, include_domains: list = None):
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
            print(f"Tavily search error: {e}")
            return []

    def search_ddgs(self, query: str):
        # DuckDuckGo fallback
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=15))
            return [{"title": r['title'], "url": r['href'], "content": r['body'], "score": 0.5} for r in results]
        except Exception as e:
            print(f"DuckDuckGo search error: {e}")
            return []

    def parallel_search(self, query: str, include_domains: list = None):
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_tavily = executor.submit(self.search_tavily, query, include_domains)
            future_ddgs = executor.submit(self.search_ddgs, query)
            
            tavily_results = future_tavily.result()
            ddgs_results = future_ddgs.result()
            
            return tavily_results + ddgs_results

search_provider = SearchProvider()
