import asyncio

import pytest

from pipeline.workers import search_worker
from pipeline.workers.search_worker import SearchError, multi_search, search_brave


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class FakeAsyncClient:
    responses: list[FakeResponse] = []
    requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, url, *, headers, params=None, **kwargs):
        self.requests.append(
            {"method": "GET", "url": url, "headers": headers, "params": params}
        )
        return self.responses.pop(0)

    async def post(self, url, *, headers=None, json=None, **kwargs):
        self.requests.append(
            {"method": "POST", "url": url, "headers": headers, "json": json}
        )
        return self.responses.pop(0)


def test_multi_search_deduplicates_overlapping_urls(monkeypatch) -> None:
    FakeAsyncClient.responses = [
        FakeResponse(
            200,
            {
                "web": {
                    "results": [
                        {
                            "url": "https://example.com/a",
                            "title": "A",
                            "description": "First A",
                        },
                        {
                            "url": "https://example.com/shared",
                            "title": "Shared",
                            "description": "First shared",
                        },
                    ]
                }
            },
        ),
        FakeResponse(
            200,
            {
                "web": {
                    "results": [
                        {
                            "url": "https://example.com/shared",
                            "title": "Shared",
                            "description": "Second shared",
                        },
                        {
                            "url": "https://example.com/b",
                            "title": "B",
                            "description": "First B",
                        },
                    ]
                }
            },
        ),
    ]
    FakeAsyncClient.requests = []
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-token")
    # Ensure Tavily and Exa are absent so only Brave fires (avoids 4-request count).
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(search_worker.httpx, "AsyncClient", FakeAsyncClient)

    results = asyncio.run(multi_search(["alpha", "beta"]))

    assert [result.url for result in results] == [
        "https://example.com/a",
        "https://example.com/shared",
        "https://example.com/b",
    ]
    assert len(FakeAsyncClient.requests) == 2
    assert all(
        request["headers"] == {"X-Subscription-Token": "test-token"}
        for request in FakeAsyncClient.requests
    )


def test_search_error_raised_on_401(monkeypatch) -> None:
    FakeAsyncClient.responses = [FakeResponse(401, {"message": "Unauthorized"})]
    FakeAsyncClient.requests = []
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "bad-token")
    monkeypatch.setattr(search_worker.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(SearchError, match="401"):
        asyncio.run(search_brave("database indexing"))


# ── Versioned documentation dedup ────────────────────────────────────

def test_doc_version_identity_groups_releases_and_orders_them():
    """Every release of one page shares an identity; ranks sort oldest
    to newest, including the compressed forms Kafka uses (0100 = 0.10.0)."""
    from pipeline.workers.search_worker import doc_version_identity

    ids = [doc_version_identity(f"https://kafka.apache.org/{v}/design/design")
           for v in ("0100", "30", "43")]
    assert len({i[0] for i in ids}) == 1, "same page must share one identity"
    assert ids[0][1] < ids[1][1] < ids[2][1], "0.10.0 < 3.0 < 4.3"

    # Named channels are the newest by definition.
    _, current = doc_version_identity("https://docs.confluent.io/platform/current/x.html")
    assert current > ids[2][1]

    # A URL with no version segment is left alone.
    _, none = doc_version_identity("https://example.com/blog/post")
    assert none == (-1.0,)


def test_dedupe_doc_versions_keeps_only_the_newest_of_each_page():
    """The defect this prevents: one design doc entering the evidence set
    three times and leaving as three numbered references."""
    from pipeline.workers.search_worker import dedupe_doc_versions, SearchResult

    results = [
        SearchResult(url="https://kafka.apache.org/0100/design/design", title="a", snippet=""),
        SearchResult(url="https://kafka.apache.org/43/design/design", title="b", snippet=""),
        SearchResult(url="https://kafka.apache.org/30/design/design", title="c", snippet=""),
        SearchResult(url="https://kafka.apache.org/25/operations/monitoring", title="d", snippet=""),
        SearchResult(url="https://factorhouse.io/articles/kafka", title="e", snippet=""),
    ]
    kept = [r.url for r in dedupe_doc_versions(results)]
    assert kept == [
        "https://kafka.apache.org/43/design/design",       # newest design page
        "https://kafka.apache.org/25/operations/monitoring",  # only release present
        "https://factorhouse.io/articles/kafka",           # unversioned, untouched
    ]


def test_personal_github_io_is_not_documentation_grade():
    """A hobby site on github.io outranked the official config reference
    for a version-specific API claim; user hosting is not a publisher."""
    from pipeline.workers.extraction_worker import score_url

    assert score_url("https://advanced-beginner.github.io/en/docs/kafka/x") < 0.7
    # Official docs for the topic still win outright.
    assert score_url("https://kafka.apache.org/43/design/design",
                     frozenset({"kafka.apache.org"})) == 1.0


def test_newest_version_by_host_and_url_upgrade():
    """A lone hit on an old release survives dedup (nothing newer of THAT
    page is present), so the fetcher upgrades it to the newest release the
    host is serving anywhere in the result set."""
    from pipeline.workers.search_worker import (
        SearchResult, newest_version_by_host, upgrade_doc_url,
    )

    results = [
        SearchResult(url="https://kafka.apache.org/43/design/design", title="a", snippet=""),
        SearchResult(url="https://kafka.apache.org/0101/javadoc/KafkaConsumer.html", title="b", snippet=""),
        SearchResult(url="https://example.com/post", title="c", snippet=""),
    ]
    newest = newest_version_by_host(results)
    assert newest == {"kafka.apache.org": "43"}

    assert upgrade_doc_url("https://kafka.apache.org/0101/javadoc/KafkaConsumer.html", newest) \
        == "https://kafka.apache.org/43/javadoc/KafkaConsumer.html"
    # Already newest, or not versioned: nothing to do.
    assert upgrade_doc_url("https://kafka.apache.org/43/design/design", newest) is None
    assert upgrade_doc_url("https://example.com/post", newest) is None


def test_process_search_result_falls_back_when_upgrade_404s(monkeypatch):
    """Not every page survives every release: if the newer URL is missing,
    the original must still be fetched rather than the source dropped."""
    import asyncio
    from pipeline.workers import extraction_worker as ew

    tried = []

    async def fake_fetch(url, *a, **k):
        tried.append(url)
        if "/43/" in url:
            raise ew.FetchError("gone", status_code=404)
        return "Kafka consumer position semantics.", "direct"

    monkeypatch.setattr(ew, "fetch_with_retry", fake_fetch)
    spans = asyncio.run(ew.process_search_result(
        ew.SearchResult(url="https://kafka.apache.org/0101/javadoc/X.html", title="t", snippet=""),
        newest_by_host={"kafka.apache.org": "43"},
    ))
    assert tried == [
        "https://kafka.apache.org/43/javadoc/X.html",
        "https://kafka.apache.org/0101/javadoc/X.html",
    ]
    assert spans and spans[0].source_url == "https://kafka.apache.org/0101/javadoc/X.html"
