import json
import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel

from pipeline.cache import StageCache
from pipeline.schemas.models import ArticleRequest, EvidenceSpan, StoryBrief


@pytest.fixture
def tmp_cache_dir() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def test_cache_miss_returns_none(tmp_cache_dir) -> None:
    cache = StageCache(cache_dir=tmp_cache_dir)
    assert cache.get("brief", "any-topic") is None


def test_cache_round_trip_with_pydantic_model(tmp_cache_dir) -> None:
    cache = StageCache(cache_dir=tmp_cache_dir)
    brief = StoryBrief(
        thesis="Caches save money.",
        angle="deep-dive",
        reader_pain_point="Repeated work.",
        key_insight="Hash by content.",
        hook_seed="A failed run cost you $5.",
        suggested_title="How to Cache Pipelines",
    )

    cache.set("brief", brief, "topic-A")
    raw = cache.get("brief", "topic-A")
    assert raw is not None
    assert StoryBrief.model_validate(raw) == brief


def test_cache_keys_are_input_sensitive(tmp_cache_dir) -> None:
    """Different inputs produce different keys; same inputs hit the same entry."""
    cache = StageCache(cache_dir=tmp_cache_dir)
    cache.set("brief", {"x": 1}, "topic-A", "level-1")
    cache.set("brief", {"x": 2}, "topic-A", "level-2")

    assert cache.get("brief", "topic-A", "level-1") == {"x": 1}
    assert cache.get("brief", "topic-A", "level-2") == {"x": 2}
    assert cache.get("brief", "topic-B", "level-1") is None


def test_cache_handles_list_of_pydantic_models(tmp_cache_dir) -> None:
    cache = StageCache(cache_dir=tmp_cache_dir)
    spans = [
        EvidenceSpan(
            source_url=f"https://example.com/{i}",
            source_title=f"Source {i}",
            content=f"Content {i}",
            trust_score=0.8,
        )
        for i in range(3)
    ]

    cache.set("search", spans, "topic-A")
    raw = cache.get("search", "topic-A")
    assert isinstance(raw, list)
    assert len(raw) == 3
    restored = [EvidenceSpan.model_validate(s) for s in raw]
    # UUIDs survive round-trip — critical for downstream stages to match span IDs.
    assert [str(s.span_id) for s in restored] == [str(s.span_id) for s in spans]


def test_cache_accepts_pydantic_input_as_key_part(tmp_cache_dir) -> None:
    """Pydantic models in cache key parts hash by their JSON representation."""
    cache = StageCache(cache_dir=tmp_cache_dir)
    request_a = ArticleRequest(topic="foo")
    request_b = ArticleRequest(topic="bar")

    cache.set("planning", {"sections": 3}, request_a)
    assert cache.get("planning", request_a) == {"sections": 3}
    assert cache.get("planning", request_b) is None


def test_disabled_cache_is_no_op(tmp_cache_dir, monkeypatch) -> None:
    monkeypatch.setenv("ARTICLE_CACHE", "0")
    cache = StageCache(cache_dir=tmp_cache_dir)
    cache.set("brief", {"x": 1}, "topic-A")
    # Nothing was written; nothing returned.
    assert cache.get("brief", "topic-A") is None


def test_cache_handles_dict_with_pydantic_values(tmp_cache_dir) -> None:
    """Verification stage caches a dict with plan, spans, reports — make sure nested models serialize."""
    cache = StageCache(cache_dir=tmp_cache_dir)
    span = EvidenceSpan(
        source_url="https://example.com",
        source_title="X",
        content="Y",
        trust_score=0.9,
    )
    value = {"meta": "ok", "spans": [span]}

    cache.set("verification", value, "key-1")
    raw = cache.get("verification", "key-1")

    assert raw["meta"] == "ok"
    restored = EvidenceSpan.model_validate(raw["spans"][0])
    assert restored.source_url == span.source_url
