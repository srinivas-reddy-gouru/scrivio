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
