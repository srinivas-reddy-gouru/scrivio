import asyncio
import logging
import os
import re
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

import httpx
from pydantic import BaseModel


# Query-string params added by ad/tracking systems that don't change the page.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "via", "fbclid", "gclid", "mc_cid", "mc_eid",
    "yclid", "gbraid", "wbraid", "_ga", "msclkid",
})


# Documentation sites publish the same page once per release
# (kafka.apache.org/0100/design/design, /30/…, /43/…). Left alone, one page
# enters the evidence set several times, burns fetch budget, and comes out
# of citation resolution as three separate numbered references to what a
# reader would call "the Kafka design doc". Worse, the oldest copy often
# ranks first, so an article about current behaviour cites decade-old docs.
_VERSION_SEGMENT = re.compile(
    r"^(?:v?\d{1,4}(?:[._]\d+){0,3}|latest|current|stable|master|main)$",
    re.IGNORECASE,
)
_UNVERSIONED = (-1.0,)


def _version_rank(segment: str) -> tuple[float, ...]:
    """Sortable rank for a version path segment. Named channels outrank
    numbers because "latest" is by definition the current release."""
    seg = segment.lower().lstrip("v")
    if seg in ("latest", "current", "stable", "master", "main"):
        return (float("inf"),)
    parts = re.split(r"[._]", seg)
    if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) >= 2:
        # Compressed forms: "0100" is 0.10.0, "43" is 4.3, "25" is 2.5.
        digits = parts[0]
        if digits.startswith("0") and len(digits) >= 3:
            return (0.0, float(digits[1:3]), float(digits[3:] or 0))
        return tuple(float(d) for d in digits)
    return tuple(float(p) if p.isdigit() else 0.0 for p in parts)


def doc_version_identity(url: str) -> tuple[str, tuple[float, ...]]:
    """Split a documentation URL into (page identity, version rank).

    The identity is the URL with its version segment removed, so every
    release of one page shares it; the rank orders those releases."""
    try:
        parsed = urlparse(canonical_url(url))
    except Exception:
        return url, _UNVERSIONED
    segments = [seg for seg in parsed.path.split("/") if seg]
    for i, seg in enumerate(segments):
        if _VERSION_SEGMENT.match(seg):
            rest = segments[:i] + segments[i + 1:]
            identity = f"{parsed.netloc}/{'/'.join(rest)}"
            return identity, _version_rank(seg)
    return f"{parsed.netloc}{parsed.path}", _UNVERSIONED


def dedupe_doc_versions(results: list) -> list:
    """Keep one result per documentation page: the newest release of it.

    Order is preserved so trust ranking upstream still decides priority."""
    best: dict[str, tuple[float, ...]] = {}
    for r in results:
        identity, rank = doc_version_identity(r.url)
        if rank == _UNVERSIONED:
            continue
        if identity not in best or rank > best[identity]:
            best[identity] = rank
    kept, seen = [], set()
    for r in results:
        identity, rank = doc_version_identity(r.url)
        if rank == _UNVERSIONED:
            kept.append(r)
            continue
        if rank == best[identity] and identity not in seen:
            seen.add(identity)
            kept.append(r)
    return kept


def newest_version_by_host(results: list) -> dict[str, str]:
    """Highest version segment seen per host across a result set.

    Dedup only removes a stale page when a newer copy of THAT page is also
    in the results. A lone hit on an old release (a 0.10.1 javadoc when
    the rest of the evidence is 4.3) survives, and the article ends up
    citing decade-old docs for current behaviour. Knowing the newest
    release a host is serving lets the fetcher try that one instead."""
    newest: dict[str, tuple[tuple[float, ...], str]] = {}
    for r in results:
        try:
            parsed = urlparse(r.url)
        except Exception:
            continue
        for seg in parsed.path.split("/"):
            if seg and _VERSION_SEGMENT.match(seg):
                rank = _version_rank(seg)
                host = parsed.netloc.lower()
                if host not in newest or rank > newest[host][0]:
                    newest[host] = (rank, seg)
                break
    return {host: seg for host, (_, seg) in newest.items()}


def upgrade_doc_url(url: str, newest_by_host: dict[str, str]) -> str | None:
    """Rewrite a versioned doc URL to the newest release that host serves.

    Returns None when there is nothing to upgrade. The caller must fall
    back to the original if the rewritten URL does not exist: not every
    page survives every release."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    target = newest_by_host.get(parsed.netloc.lower())
    if not target:
        return None
    segments = parsed.path.split("/")
    for i, seg in enumerate(segments):
        if seg and _VERSION_SEGMENT.match(seg):
            if _version_rank(seg) >= _version_rank(target):
                return None
            segments[i] = target
            return urlunparse(parsed._replace(path="/".join(segments)))
    return None


_TITLE_VERSION = re.compile(r"\s*\bv?\d+\.\d+(?:\.\d+)?\b")


def strip_stale_version_from_title(title: str) -> str:
    """Drop the release number from a title whose URL has been upgraded.

    Search engines return the title of the page they indexed, so after a
    URL is rewritten to the current release the reference would read
    "KafkaConsumer (kafka 2.7.0 API)" while linking to 4.1: a reader
    would reasonably believe they were getting 2.7 docs. Removing the
    number is honest; inventing the new one would not be."""
    cleaned = _TITLE_VERSION.sub("", title)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -|·")


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


def _brave_domain_query(query: str, include_domains: list[str] | None) -> str:
    """Brave has no domain-filter parameter — express the restriction with
    site: operators in the query string. Capped at 3 domains: Brave's
    operators are experimental and long OR chains reduce recall."""
    if not include_domains:
        return query
    sites = " OR ".join(f"site:{d}" for d in include_domains[:3])
    return f"{query} ({sites})" if len(include_domains) > 1 else f"{query} site:{include_domains[0]}"


async def search_brave(
    query: str, max_results: int = 8, include_domains: list[str] | None = None
) -> list[SearchResult]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"]},
            params={"q": _brave_domain_query(query, include_domains), "count": max_results},
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


async def search_exa(
    query: str, max_results: int = 8, include_domains: list[str] | None = None
) -> list[SearchResult]:
    payload: dict = {
        "query": query,
        "numResults": max_results,
        "useAutoprompt": True,
        "type": "neural",
    }
    if include_domains:
        payload["includeDomains"] = include_domains
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": os.environ["EXA_API_KEY"]},
            json=payload,
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


async def search_tavily(
    query: str, max_results: int = 8, include_domains: list[str] | None = None
) -> list[SearchResult]:
    """Tavily Search — free tier: 1000 queries/month, no credit card required.
    Sign up at app.tavily.com and set TAVILY_API_KEY.
    """
    payload: dict = {
        "api_key": os.environ["TAVILY_API_KEY"],
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": False,
    }
    if include_domains:
        payload["include_domains"] = include_domains
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json=payload,
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


async def _claude_cli_search(
    query: str, include_domains: list[str] | None = None
) -> list[SearchResult]:
    """Search via the Claude CLI's WebSearch tool (subscription-powered).
    Domain restriction is expressed with site: operators, same as Brave."""
    from pipeline.providers.claude_cli_adapter import cli_web_search

    results = await cli_web_search(_brave_domain_query(query, include_domains))
    return [
        SearchResult(
            url=r["url"],
            title=str(r.get("title") or ""),
            snippet=str(r.get("snippet") or ""),
            published_at=r.get("published_at") or None,
        )
        for r in results
    ]


async def multi_search(
    queries: list[str],
    provider: str | None = None,
    include_domains: list[str] | None = None,
) -> list[SearchResult]:
    """Run queries against available search providers.

    Passing provider="brave", "exa", or "tavily" forces a specific provider.
    When provider is None (default), all providers with configured API keys
    are used and results are merged and deduplicated by URL.

    include_domains restricts results to the given domains — used by the
    docs-first pass to pull evidence from official documentation sites.
    (Tavily/Exa support it natively; Brave gets site: operators in the query.)

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
        # BYO-subscription fallback: with no search API key, real web
        # search runs through the Claude CLI's built-in WebSearch tool
        # (slower and model-mediated, but genuinely live). Only when the
        # CLI is installed; otherwise search degrades to nothing, as before.
        from pipeline.providers.claude_cli_adapter import (
            claude_cli_available as _cli_ok,
        )
        if _cli_ok():
            search_fns = [_claude_cli_search]
        else:
            return []

    tasks = [
        fn(q, include_domains=include_domains) for fn in search_fns for q in queries
    ]
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
