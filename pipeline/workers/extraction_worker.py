import asyncio
import logging
import re
import os
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from langchain_text_splitters import RecursiveCharacterTextSplitter

from pipeline.schemas.models import EvidenceSpan
from pipeline.workers.search_worker import SearchResult


REDACTION_TEXT = "[REDACTED — injection attempt detected]"
START_PATTERNS = ("ignore", "disregard", "forget")
CONTAINS_PATTERNS = ("you are now", "new instructions", "ignore previous")
XML_TAGS = ("system", "prompt", "instructions", "context", "override")

# Limits concurrent outbound HTTP fetches so we don't hammer servers or get
# IP-blocked. Module-level; safe because asyncio is single-threaded.
_fetch_semaphore: asyncio.Semaphore | None = None

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# HTTP status codes where retrying or trying another strategy is pointless.
# 4xx errors are client-side: the URL doesn't exist, we're blocked, etc.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 410, 451})

# ── Trust tiers ─────────────────────────────────────────────────────────────
# Citation precedence the pipeline enforces (highest wins fetch budget and
# planner evidence slots):
#   1.0   official docs for THIS topic (dynamic, from source_resolver)
#   0.9   curated always-trustworthy domains (academic, vendor docs, refs)
#   0.8   URLs that look like documentation (docs.* hosts, /docs paths, …)
#   0.7   github.com (source code and issue threads)
#   0.65  blog platforms (useful colour, weaker authority)
#   0.6   unknown HTTPS
#   0.45  Q&A forums — deliberately BELOW unknown articles: answers are
#         unreviewed and the user wants docs > articles > Stack Overflow
#   0.35  plain HTTP

_HIGH_TRUST_DOMAINS = frozenset({
    # Academic / standards bodies
    "arxiv.org", "dl.acm.org", "ieee.org", "nature.com", "science.org",
    "ncbi.nlm.nih.gov",
    # Official language / runtime docs
    "docs.python.org", "developer.mozilla.org", "docs.oracle.com",
    "openjdk.org", "jcp.org",
    # Cloud provider official docs
    "docs.aws.amazon.com", "docs.anthropic.com", "openai.com",
    "docs.microsoft.com", "learn.microsoft.com", "cloud.google.com",
    # Infrastructure official docs
    "kubernetes.io", "docs.docker.com", "postgresql.org",
    "redis.io", "kafka.apache.org", "nginx.org", "prometheus.io",
    # JVM / Java ecosystem official sites
    "docs.spring.io", "spring.io",          # official Spring documentation
    "baeldung.com",                          # de-facto reference for Spring/Java
    "resilience4j.readme.io",               # official Resilience4j docs
    "quarkus.io", "micronaut.io",           # official JVM framework docs
    "javadoc.io",                            # aggregated Java API docs
    # Trusted engineering blogs
    "research.google.com", "ai.googleblog.com", "blog.google",
    "engineering.atspotify.com", "netflixtechblog.com", "engineering.fb.com",
    "blog.cloudflare.com", "martinfowler.com", "infoq.com",
})

_MEDIUM_TRUST_DOMAINS = frozenset({
    "github.com",
})

_BLOG_PLATFORM_DOMAINS = frozenset({
    "medium.com", "dev.to", "hashnode.dev", "substack.com",
})

_QA_FORUM_DOMAINS = frozenset({
    "stackoverflow.com", "stackexchange.com", "serverfault.com",
    "superuser.com", "reddit.com", "quora.com", "news.ycombinator.com",
})

# Heuristic markers of official-documentation URLs on domains we've never
# seen before. Covers the long tail the curated list can't: docs.<vendor>,
# <project>.readthedocs.io, <vendor>.com/docs/…
_DOCS_HOST_PREFIXES = ("docs.", "doc.", "developer.", "developers.", "wiki.")
_DOCS_HOST_SUFFIXES = (".readthedocs.io", ".github.io")
_DOCS_PATH_PREFIXES = (
    "/docs", "/documentation", "/reference", "/manual", "/javadoc",
    "/apidocs", "/api-reference", "/guide", "/guides",
)

# Jina AI Reader converts any URL to clean plain text, including JS-rendered
# pages. Free, no API key needed. Set USE_JINA_READER=true to activate.
# Jina also offers a paid tier with higher rate limits via JINA_API_KEY.
_JINA_BASE = "https://r.jina.ai/"


class FetchError(Exception):
    """Raised when a page fetch fails. `status_code` is set when the failure
    came from an HTTP response; None for network / timeout errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_permanent(self) -> bool:
        """True if retrying the same URL with the same fetcher is pointless."""
        return self.status_code in _PERMANENT_STATUSES


def _get_semaphore() -> asyncio.Semaphore:
    global _fetch_semaphore
    if _fetch_semaphore is None:
        _fetch_semaphore = asyncio.Semaphore(5)
    return _fetch_semaphore


async def fetch_page(url: str) -> str:
    """Fetch a URL with a browser-like User-Agent, respecting the concurrency cap."""
    async with _get_semaphore():
        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, timeout=12, follow_redirects=True
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise FetchError(f"Timed out fetching {url}") from exc
        except httpx.RequestError as exc:
            raise FetchError(f"Request error for {url}: {exc}") from exc

    if response.status_code != 200:
        raise FetchError(
            f"Fetch failed for {url} with status {response.status_code}",
            status_code=response.status_code,
        )

    return response.text


# Circuit breaker for keyless Jina calls. Jina Reader returns 401 without an
# API key; once we see that, every further keyless attempt this run is wasted
# latency and log noise — fail fast instead.
_jina_auth_failed = False


async def fetch_page_jina(url: str) -> str:
    """Fetch via Jina AI Reader — handles JS-rendered pages.

    Requires JINA_API_KEY (Jina returns 401 for keyless requests).
    Used as the fallback fetcher; set USE_JINA_READER=true to try it first.
    """
    global _jina_auth_failed
    jina_key = os.environ.get("JINA_API_KEY")
    if _jina_auth_failed and not jina_key:
        raise FetchError(
            f"Jina Reader unauthorized earlier this run (no JINA_API_KEY) — skipping {url}",
            status_code=401,
        )

    jina_url = f"{_JINA_BASE}{url}"
    headers = dict(_HEADERS)
    headers["Accept"] = "text/plain"
    if jina_key:
        headers["Authorization"] = f"Bearer {jina_key}"

    async with _get_semaphore():
        try:
            async with httpx.AsyncClient(
                headers=headers, timeout=20, follow_redirects=True
            ) as client:
                response = await client.get(jina_url)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise FetchError(f"Jina fetch failed for {url}: {exc}") from exc

    if response.status_code != 200:
        if response.status_code == 401 and not jina_key:
            _jina_auth_failed = True
            logging.info(
                "Jina Reader requires an API key (got 401 keyless). "
                "Set JINA_API_KEY in Settings to enable the fallback fetcher; "
                "skipping Jina for the rest of this run."
            )
        raise FetchError(
            f"Jina fetch failed for {url} with status {response.status_code}",
            status_code=response.status_code,
        )

    return response.text


async def fetch_with_retry(url: str, max_attempts: int = 2) -> tuple[str, str]:
    """Fetch a page using direct HTML first, then Jina Reader as fallback.

    Strategy:
    - Try the primary fetcher with bounded retries (transient errors only).
    - On a 4xx permanent error (403/451/404 — site blocks scrapers or page
      gone), skip retries and move directly to the fallback fetcher.
    - The fallback gets its own retry budget for transient errors.

    Set USE_JINA_READER=true to flip the order (Jina first, direct fallback).
    This is useful for sites that block direct scrapers but allow Jina.
    """
    prefer_jina = os.environ.get("USE_JINA_READER", "").lower() in ("1", "true", "yes")
    strategies = (
        [("jina", fetch_page_jina), ("direct", fetch_page)]
        if prefer_jina
        else [("direct", fetch_page), ("jina", fetch_page_jina)]
    )

    last_exc: FetchError = FetchError("no attempts made")
    for name, fetch_fn in strategies:
        for attempt in range(max_attempts):
            try:
                text = await fetch_fn(url)
                return text, name
            except FetchError as exc:
                last_exc = exc
                # Permanent client errors: stop retrying this strategy and try
                # the next one immediately.
                if exc.is_permanent:
                    logging.info(
                        "%s fetch hit permanent error %s for %s — switching strategy",
                        name, exc.status_code, url,
                    )
                    break
                if attempt < max_attempts - 1:
                    backoff = 2 ** attempt
                    logging.warning(
                        "%s fetch attempt %d/%d failed for %s: %s — retrying in %ds",
                        name, attempt + 1, max_attempts, url, exc, backoff,
                    )
                    await asyncio.sleep(backoff)

    raise last_exc


# Control characters (except tab/newline/CR) are illegal in XML; some pages
# embed them and crash readability's lxml cleaner. Strip before parsing.
_XML_ILLEGAL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# When readability distills a large page to less than this, it almost
# certainly latched onto the wrong node (GitHub wiki pages: 282KB of HTML
# → 43 chars of "You can't perform that action at this time."). Re-extract
# from the whole document instead of silently losing the source.
_MIN_PLAUSIBLE_EXTRACTION_CHARS = 400


def _strip_and_text(soup: BeautifulSoup) -> str:
    for tag in soup(["nav", "footer", "script", "style", "header", "aside"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def remove_boilerplate(html: str) -> str:
    """Extract main content from raw HTML, stripping nav/footer/scripts."""
    html = _XML_ILLEGAL_CHARS.sub("", html)
    try:
        from readability import Document

        content_html = Document(html).summary()
        text = _strip_and_text(BeautifulSoup(content_html, "html.parser"))
    except Exception:
        return _strip_and_text(BeautifulSoup(html, "html.parser"))

    # Plausibility guard: a tiny distillation of a large page means
    # readability picked a hidden notice/error div, not the article.
    if (
        len(text) < _MIN_PLAUSIBLE_EXTRACTION_CHARS
        and len(html) > 10 * _MIN_PLAUSIBLE_EXTRACTION_CHARS
    ):
        logging.info(
            "readability produced %d chars from %d chars of HTML — "
            "falling back to whole-document extraction.", len(text), len(html),
        )
        return _strip_and_text(BeautifulSoup(html, "html.parser"))

    return text


def injection_filter(text: str) -> str:
    filtered_lines = []

    for line in text.splitlines():
        if _is_injection_line(line):
            logging.warning("Redacted possible prompt injection line: %s", line)
            filtered_lines.append(REDACTION_TEXT)
        else:
            filtered_lines.append(line)

    return "\n".join(filtered_lines)


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
    )
    return splitter.split_text(text)


def _domain_in(domain: str, domains) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in domains)


def _looks_like_docs(domain: str, path: str) -> bool:
    """Heuristic: does this URL look like official documentation?"""
    if domain.startswith(_DOCS_HOST_PREFIXES):
        return True
    if domain.endswith(_DOCS_HOST_SUFFIXES):
        return True
    return path.startswith(_DOCS_PATH_PREFIXES)


def score_url(url: str, official_domains: frozenset[str] = frozenset()) -> float:
    """Trust-score a URL. *official_domains* is the per-topic set resolved by
    source_resolver — those are the article's source of truth and outrank
    everything, including the curated list."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().removeprefix("www.")
        path = parsed.path.lower()
    except Exception:
        return 0.5

    if official_domains and _domain_in(domain, official_domains):
        return 1.0
    if _domain_in(domain, _HIGH_TRUST_DOMAINS):
        return 0.9
    # Q&A forums before the docs heuristic so forum paths can't sneak up a tier.
    if _domain_in(domain, _QA_FORUM_DOMAINS):
        return 0.45
    if _looks_like_docs(domain, path):
        return 0.8
    if _domain_in(domain, _MEDIUM_TRUST_DOMAINS):
        return 0.7
    if _domain_in(domain, _BLOG_PLATFORM_DOMAINS):
        return 0.65
    return 0.6 if url.startswith("https://") else 0.35


def build_evidence_spans(
    url, title, chunks, published_at=None,
    official_domains: frozenset[str] = frozenset(),
) -> list[EvidenceSpan]:
    trust_score = score_url(url, official_domains)
    parsed_published_at = _parse_published_at(published_at)

    return [
        EvidenceSpan(
            source_url=url,
            source_title=title,
            content=chunk,
            published_at=parsed_published_at,
            trust_score=trust_score,
            was_filtered=REDACTION_TEXT in chunk,
        )
        for chunk in chunks
    ]


async def process_search_result(
    result: SearchResult,
    official_domains: frozenset[str] = frozenset(),
) -> list[EvidenceSpan]:
    try:
        raw, strategy = await fetch_with_retry(result.url)
    except FetchError as exc:
        logging.warning("Skipping %s after all fetch attempts: %s", result.url, exc)
        return []

    # Jina already returns clean plain text/markdown; only raw HTML from the
    # direct fetcher needs boilerplate stripping (nav / footer / scripts).
    text = raw if strategy == "jina" else remove_boilerplate(raw)

    filtered_text = injection_filter(text)
    chunks = chunk_text(filtered_text)
    return build_evidence_spans(
        result.url,
        result.title,
        chunks,
        published_at=result.published_at,
        official_domains=official_domains,
    )


def _is_injection_line(line: str) -> bool:
    normalized = line.strip().lower()

    if normalized.startswith(START_PATTERNS):
        return True

    if any(pattern in normalized for pattern in CONTAINS_PATTERNS):
        return True

    if normalized.startswith("```system"):
        return True

    return any(
        f"<{tag}>" in normalized or f"</{tag}>" in normalized for tag in XML_TAGS
    )


def _parse_published_at(published_at):
    if published_at is None or isinstance(published_at, datetime):
        return published_at

    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None
