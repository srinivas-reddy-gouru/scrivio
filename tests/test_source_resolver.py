"""Tests for topic → official-docs domain resolution and the doc-first
search plumbing built on top of it."""
import asyncio
from types import SimpleNamespace

from pipeline.workers.search_worker import SearchResult, _brave_domain_query
from pipeline.workers.source_resolver import (
    MAX_OFFICIAL_DOMAINS,
    _normalize_domain,
    resolve_official_sources,
    static_official_sources,
)


class _FakeToolClient:
    """Anthropic-shaped client that returns a canned tool_use response."""

    def __init__(self, domains):
        self.messages = self
        self._domains = domains
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="submit_official_sources",
                    input={"domains": self._domains},
                )
            ]
        )


class _ExplodingClient:
    def __init__(self):
        self.messages = self

    async def create(self, **kwargs):
        raise RuntimeError("provider down")


def test_static_sources_match_on_word_boundaries() -> None:
    assert static_official_sources("Kafka consumer rebalancing") == ["kafka.apache.org"]
    assert "docs.oracle.com" in static_official_sources("Java virtual threads deep dive")
    # "javascript" must not trigger the "java" entry.
    assert "docs.oracle.com" not in static_official_sources("javascript closures")
    assert static_official_sources("stoicism in daily life") == []


def test_normalize_domain_strips_scheme_www_and_path() -> None:
    assert _normalize_domain("https://docs.oracle.com/javase/") == "docs.oracle.com"
    assert _normalize_domain("www.kafka.apache.org") == "kafka.apache.org"
    assert _normalize_domain("react.dev") == "react.dev"
    assert _normalize_domain("not a domain") is None
    assert _normalize_domain("") is None


def test_resolver_merges_static_and_llm_domains() -> None:
    client = _FakeToolClient(["https://kafka.apache.org/docs", "docs.confluent.io"])
    domains = asyncio.run(
        resolve_official_sources("Kafka backpressure", None, client)
    )
    # Static seed first, LLM extras after, duplicates collapsed.
    assert domains[0] == "kafka.apache.org"
    assert "docs.confluent.io" in domains
    assert len(domains) == len(set(domains)) <= MAX_OFFICIAL_DOMAINS


def test_resolver_rejects_invalid_llm_domains() -> None:
    client = _FakeToolClient(["not a domain!", "<script>", "valid.example.org"])
    domains = asyncio.run(resolve_official_sources("some obscure tool", None, client))
    assert domains == ["valid.example.org"]


def test_resolver_degrades_to_static_on_llm_failure() -> None:
    domains = asyncio.run(
        resolve_official_sources("Kafka streams", None, _ExplodingClient())
    )
    assert domains == ["kafka.apache.org"]


def test_brave_domain_query_builds_site_operators() -> None:
    assert _brave_domain_query("kafka tuning", None) == "kafka tuning"
    assert (
        _brave_domain_query("kafka tuning", ["kafka.apache.org"])
        == "kafka tuning site:kafka.apache.org"
    )
    multi = _brave_domain_query("kafka", ["a.org", "b.org", "c.org", "d.org"])
    assert multi == "kafka (site:a.org OR site:b.org OR site:c.org)"


def test_select_fetch_candidates_caps_official_docs_at_half_budget() -> None:
    from main import _select_fetch_candidates

    official = frozenset({"kafka.apache.org"})
    results = [
        SearchResult(url=f"https://kafka.apache.org/doc{i}", title="", snippet="")
        for i in range(8)
    ] + [
        SearchResult(url=f"https://blog{i}.example.com/post", title="", snippet="")
        for i in range(8)
    ]
    selected, skipped = _select_fetch_candidates(results, official, 10)
    official_count = sum(1 for r in selected if "kafka.apache.org" in r.url)
    assert len(selected) == 10
    assert official_count == 5  # capped at half, not all 8
    assert len(skipped) == 6


def test_select_fetch_candidates_backfills_when_general_is_thin() -> None:
    from main import _select_fetch_candidates

    official = frozenset({"kafka.apache.org"})
    results = [
        SearchResult(url=f"https://kafka.apache.org/doc{i}", title="", snippet="")
        for i in range(8)
    ] + [
        SearchResult(url="https://blog.example.com/post", title="", snippet="")
    ]
    selected, _ = _select_fetch_candidates(results, official, 10)
    # Only 1 general result exists, so officials fill 8 of the 10 slots.
    assert sum(1 for r in selected if "kafka.apache.org" in r.url) == 8
    assert len(selected) == 9


# ── Per-request provider pin (main._resolve_provider) ──────────────────────

def test_resolve_provider_honors_request_pin(monkeypatch) -> None:
    from main import _resolve_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert _resolve_provider("openai") == "openai"
    assert _resolve_provider("anthropic") == "anthropic"
    # auto with both keys defaults to anthropic
    assert _resolve_provider("auto") == "anthropic"


def test_resolve_provider_pin_falls_back_when_key_missing(monkeypatch) -> None:
    from main import _resolve_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Pinned to openai but no key — must fall back to auto (anthropic),
    # never break the run.
    assert _resolve_provider("openai") == "anthropic"


def test_resolve_provider_pin_overrides_env_preference(monkeypatch) -> None:
    from main import _resolve_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert _resolve_provider("openai") == "openai"


def test_article_request_defaults_to_auto_provider() -> None:
    from pipeline.schemas.models import ArticleRequest

    # Old cached/saved requests without the field must still validate.
    request = ArticleRequest(topic="Kafka")
    assert request.llm_provider == "auto"


def test_normalize_domain_rejects_shared_hosting() -> None:
    # github.com/<org>/<repo> must not bless the entire host as official —
    # every random repo would score 1.0 and crowd out real sources.
    assert _normalize_domain("github.com/brettwooldridge/HikariCP") is None
    assert _normalize_domain("https://gitlab.com/some/project") is None
    assert _normalize_domain("medium.com") is None
    assert _normalize_domain("pypi.org/project/httpx/") is None
    # Per-project GitHub Pages sites remain acceptable.
    assert _normalize_domain("brettwooldridge.github.io") == "brettwooldridge.github.io"


def test_pipeline_models_reports_stage_models(monkeypatch) -> None:
    """The pipeline_info event must name the model executing each stage,
    honouring the provider pin and the adapter's tier mapping."""
    from main import _pipeline_models
    from pipeline.schemas.models import ArticleRequest

    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    monkeypatch.setenv("OPENAI_API_KEY", "o-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_STRONG_MODEL", raising=False)

    pinned_gpt = _pipeline_models(
        ArticleRequest(topic="k", model_preset="best", llm_provider="openai")
    )
    assert pinned_gpt["provider"] == "openai"
    assert pinned_gpt["stages"]["drafting"].startswith("gpt-")
    assert pinned_gpt["stages"]["verification"] == "gpt-4o-mini"

    auto = _pipeline_models(ArticleRequest(topic="k"))
    assert auto["provider"] == "anthropic"
    assert auto["stages"]["drafting"].startswith("claude-")
    # Every roadmap stage the UI shows has an entry.
    for stage in ("brief", "relevance_check", "search", "planning", "gap_fill",
                  "verification", "drafting", "editor", "polish", "critic"):
        assert auto["stages"][stage]
