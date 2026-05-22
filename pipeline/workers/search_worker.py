import asyncio
import logging
import os
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

import httpx
from pydantic import BaseModel


# Query-string params added by ad/tracking systems that don't change the page.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "via", "fbclid", "gclid", "mc_cid", "mc_eid",
    "yclid", "gbraid", "wbraid", "_ga", "msclkid",
})


def canonical_url(url: str) -> str:
    """Normalise a URL for deduplication.

    - Lowercases the scheme and host
    - Strips tracking query params
    - Removes trailing slashes from the path
    - Drops the fragment
    """
    try:
        parsed = urlparse(url)
        clean_params = urlencode(
            [(k, v) for k, v in parse_qsl(parsed.query) if k not in _TRACKING_PARAMS]
        )
        clean = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=parsed.path.rstrip("/") or "/",
            query=clean_params,
            fragment="",
        )
        return urlunparse(clean)
    except Exception:
        return url


class SearchError(Exception):
    pass


class SearchResult(BaseModel):
    url: str
    title: str
    snippet: str
    published_at: str | None = None


async def search_brave(query: str, max_results: int = 8) -> list[SearchResult]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"]},
            params={"q": query, "count": max_results},
            timeout=15,
        )

    if response.status_code != 200:
        raise SearchError(f"Brave search failed with status {response.status_code}")

    results = response.json().get("web", {}).get("results", [])
    return [
        SearchResult(
            url=result.get("url", ""),
            title=result.get("title", ""),
            snippet=result.get("description", result.get("snippet", "")),
            published_at=result.get("age")
            or result.get("published_at")
            or result.get("date"),
        )
        for result in results
    ]


async def search_exa(query: str, max_results: int = 8) -> list[SearchResult]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": os.environ["EXA_API_KEY"]},
            json={
                "query": query,
                "numResults": max_results,
                "useAutoprompt": True,
                "type": "neural",
            },
            timeout=15,
        )

    if response.status_code != 200:
        raise SearchError(f"Exa search failed with status {response.status_code}")

    results = response.json().get("results", [])
    return [
        SearchResult(
            url=result.get("url", ""),
            title=result.get("title", ""),
            snippet=result.get("text", result.get("snippet", "")),
            published_at=result.get("published_at")
            or result.get("publishedDate")
            or result.get("date"),
        )
        for result in results
    ]


async def search_tavily(query: str, max_results: int = 8) -> list[SearchResult]:
    """Tavily Search — free tier: 1000 queries/month, no credit card required.
    Sign up at app.tavily.com and set TAVILY_API_KEY.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": os.environ["TAVILY_API_KEY"],
                "query": query,
                "max_results": max_results,
                "search_depth": "advanced",
                "include_answer": False,
            },
            timeout=20,
        )

    if response.status_code != 200:
        raise SearchError(f"Tavily search failed with status {response.status_code}")

    results = response.json().get("results", [])
    return [
        SearchResult(
            url=result.get("url", ""),
            title=result.get("title", ""),
            snippet=result.get("content", result.get("snippet", "")),
            published_at=result.get("published_date"),
        )
        for result in results
    ]


_PROVIDERS: dict[str, tuple[str, object]] = {
    "brave": ("BRAVE_SEARCH_API_KEY", search_brave),
    "exa": ("EXA_API_KEY", search_exa),
    "tavily": ("TAVILY_API_KEY", search_tavily),
}


async def multi_search(
    queries: list[str], provider: str | None = None
) -> list[SearchResult]:
    """Run queries against available search providers.

    Passing provider="brave", "exa", or "tavily" forces a specific provider.
    When provider is None (default), all providers with configured API keys
    are used and results are merged and deduplicated by URL.

    For local testing with no paid keys, set TAVILY_API_KEY (free tier at
    app.tavily.com) or EXA_API_KEY (free credits on sign-up).
    """
    if provider is not None:
        env_var, fn = _PROVIDERS.get(provider, (None, None))
        if fn is None:
            raise ValueError(f"Unsupported search provider: {provider}")
        if not os.environ.get(env_var):
            raise SearchError(f"{provider} requires {env_var} to be set")
        search_fns = [fn]
    else:
        search_fns = [
            fn
            for env_var, fn in _PROVIDERS.values()
            if os.environ.get(env_var)
        ]

    if not search_fns:
        return []

    tasks = [fn(q) for fn in search_fns for q in queries]
    grouped = await asyncio.gather(*tasks, return_exceptions=True)

    results_by_canonical: dict[str, SearchResult] = {}
    for result_group in grouped:
        if isinstance(result_group, Exception):
            logging.warning("Search provider error: %s", result_group)
            continue
        for result in result_group:
            if not result.url:
                continue
            key = canonical_url(result.url)
            if key not in results_by_canonical:
                results_by_canonical[key] = result

    return list(results_by_canonical.values())
