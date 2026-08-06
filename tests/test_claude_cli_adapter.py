import asyncio
import json

import pytest

from pipeline.providers import claude_cli_adapter
from pipeline.providers.claude_cli_adapter import (
    ClaudeCLIAdapter,
    ClaudeCLIError,
    _model_alias,
)
# Bound at collection time, BEFORE the conftest hermetic fixture replaces
# main._openai_client — this reference stays the real implementation.
from main import _openai_client as real_openai_client


class _FakeProcess:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False
        self.stdin_data: bytes | None = None

    async def communicate(self, input=None):
        self.stdin_data = input
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


def _envelope(result: str) -> bytes:
    return json.dumps({"result": result, "is_error": False}).encode()


def _patch_cli(monkeypatch, responses: list[_FakeProcess]):
    """Queue fake processes; each create_subprocess_exec call pops one.
    Captures argv for assertions."""
    calls: list[list[str]] = []

    async def fake_exec(*argv, **kwargs):
        calls.append(list(argv))
        return responses.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: "claude")
    return calls


def test_model_alias_mapping(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CLI_MODEL", raising=False)
    assert _model_alias("claude-sonnet-4-6") == "sonnet"
    assert _model_alias("claude-haiku-4-5-20251001") == "haiku"
    assert _model_alias("claude-opus-4-7") == "opus"
    assert _model_alias("") == "sonnet"
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "haiku")
    assert _model_alias("claude-opus-4-7") == "haiku"


def test_text_path(monkeypatch) -> None:
    calls = _patch_cli(monkeypatch, [_FakeProcess(_envelope("Hello there."))])
    adapter = ClaudeCLIAdapter()
    response = asyncio.run(adapter.messages.create(
        model="claude-sonnet-4-6",
        system="You are a helper.",
        messages=[{"role": "user", "content": "hi"}],
    ))
    assert response.content[0].type == "text"
    assert response.content[0].text == "Hello there."
    assert "--model" in calls[0] and "sonnet" in calls[0]
    assert "--output-format" in calls[0] and "json" in calls[0]


def test_tool_path_clean_json(monkeypatch) -> None:
    payload = {"questions": []}
    _patch_cli(monkeypatch, [_FakeProcess(_envelope(json.dumps(payload)))])
    adapter = ClaudeCLIAdapter()
    response = asyncio.run(adapter.messages.create(
        model="claude-sonnet-4-6",
        system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
        tools=[{"name": "submit_x", "input_schema": {"type": "object"}}],
        tool_choice={"type": "tool", "name": "submit_x"},
        messages=[{"role": "user", "content": "go"}],
    ))
    block = response.content[0]
    assert block.type == "tool_use"
    assert block.name == "submit_x"
    assert block.input == payload


def test_tool_path_strips_code_fences(monkeypatch) -> None:
    fenced = "```json\n{\"a\": 1}\n```"
    _patch_cli(monkeypatch, [_FakeProcess(_envelope(fenced))])
    adapter = ClaudeCLIAdapter()
    response = asyncio.run(adapter.messages.create(
        model="m",
        tools=[{"name": "t", "input_schema": {}}],
        tool_choice={"type": "tool", "name": "t"},
        messages=[{"role": "user", "content": "go"}],
    ))
    assert response.content[0].input == {"a": 1}


def test_tool_path_retries_once_then_succeeds(monkeypatch) -> None:
    _patch_cli(monkeypatch, [
        _FakeProcess(_envelope("this is not json")),
        _FakeProcess(_envelope('{"fixed": true}')),
    ])
    adapter = ClaudeCLIAdapter()
    response = asyncio.run(adapter.messages.create(
        model="m",
        tools=[{"name": "t", "input_schema": {}}],
        tool_choice={"type": "tool", "name": "t"},
        messages=[{"role": "user", "content": "go"}],
    ))
    assert response.content[0].input == {"fixed": True}


def test_tool_path_fails_after_retry(monkeypatch) -> None:
    _patch_cli(monkeypatch, [
        _FakeProcess(_envelope("nope")),
        _FakeProcess(_envelope("still nope")),
    ])
    adapter = ClaudeCLIAdapter()
    with pytest.raises(ClaudeCLIError, match="invalid JSON"):
        asyncio.run(adapter.messages.create(
            model="m",
            tools=[{"name": "t", "input_schema": {}}],
            tool_choice={"type": "tool", "name": "t"},
            messages=[{"role": "user", "content": "go"}],
        ))


def test_nonzero_exit_raises(monkeypatch) -> None:
    _patch_cli(monkeypatch, [
        _FakeProcess(b"", returncode=1, stderr=b"not logged in"),
    ])
    adapter = ClaudeCLIAdapter()
    with pytest.raises(ClaudeCLIError, match="not logged in"):
        asyncio.run(adapter.messages.create(
            model="m", messages=[{"role": "user", "content": "hi"}],
        ))


def test_error_envelope_raises(monkeypatch) -> None:
    stdout = json.dumps({"result": "usage limit reached", "is_error": True}).encode()
    _patch_cli(monkeypatch, [_FakeProcess(stdout)])
    adapter = ClaudeCLIAdapter()
    with pytest.raises(ClaudeCLIError, match="usage limit"):
        asyncio.run(adapter.messages.create(
            model="m", messages=[{"role": "user", "content": "hi"}],
        ))


def test_openai_facade_create_returns_content(monkeypatch) -> None:
    from pipeline.providers.claude_cli_adapter import ClaudeCLIOpenAIFacade

    calls = _patch_cli(monkeypatch, [_FakeProcess(_envelope("a concise query"))])
    facade = ClaudeCLIOpenAIFacade()
    completion = asyncio.run(facade.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Rewrite the claim as a query."},
            {"role": "user", "content": "Kafka retains messages."},
        ],
    ))
    assert completion.choices[0].message.content == "a concise query"
    # mini-class OpenAI models map to haiku on the CLI.
    assert "haiku" in calls[0]


def test_openai_facade_parse_returns_pydantic(monkeypatch) -> None:
    from pipeline.providers.claude_cli_adapter import ClaudeCLIOpenAIFacade
    from pipeline.schemas.models import VerificationReport

    payload = {"claim_id": "c1", "support_status": "supported",
               "relevance_status": "relevant", "verifier_note": "checks out"}
    _patch_cli(monkeypatch, [_FakeProcess(_envelope(json.dumps(payload)))])
    facade = ClaudeCLIOpenAIFacade()
    completion = asyncio.run(facade.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "verify this"}],
        response_format=VerificationReport,
    ))
    parsed = completion.choices[0].message.parsed
    assert isinstance(parsed, VerificationReport)
    assert parsed.support_status == "supported"


def test_openai_facade_parse_retries_invalid_json(monkeypatch) -> None:
    from pipeline.providers.claude_cli_adapter import ClaudeCLIOpenAIFacade
    from pipeline.schemas.models import VerificationReport

    good = {"claim_id": "c1", "support_status": "weak",
            "relevance_status": "relevant", "verifier_note": "thin evidence"}
    _patch_cli(monkeypatch, [
        _FakeProcess(_envelope("not json at all")),
        _FakeProcess(_envelope(json.dumps(good))),
    ])
    facade = ClaudeCLIOpenAIFacade()
    completion = asyncio.run(facade.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "verify"}],
        response_format=VerificationReport,
    ))
    assert completion.choices[0].message.parsed.support_status == "weak"


def test_verify_claim_runs_through_facade(monkeypatch) -> None:
    """End-to-end: the real verification worker on top of the CLI facade —
    proving the fact-check stage genuinely executes on the subscription."""
    import uuid
    from pipeline.providers.claude_cli_adapter import ClaudeCLIOpenAIFacade
    from pipeline.schemas.models import Claim, EvidenceSpan
    from pipeline.workers.verification_worker import verify_claim

    span = EvidenceSpan(source_url="https://kafka.apache.org/documentation/",
                        content="Kafka retains records for a configurable period.")
    claim = Claim(text="Kafka retains messages for a configurable time.",
                  source_ids=[str(span.span_id)])
    payload = {"claim_id": str(uuid.uuid4()), "support_status": "supported",
               "relevance_status": "relevant", "verifier_note": "matches docs"}
    _patch_cli(monkeypatch, [_FakeProcess(_envelope(json.dumps(payload)))])

    report = asyncio.run(verify_claim(claim, [span], ClaudeCLIOpenAIFacade()))
    assert report.support_status == "supported"
    # claim_id is pinned to the input claim regardless of model output.
    assert report.claim_id == str(claim.claim_id)


def test_cli_web_search_parses_results(monkeypatch) -> None:
    from pipeline.providers.claude_cli_adapter import cli_web_search

    results_json = json.dumps([
        {"url": "https://kafka.apache.org/documentation/", "title": "Kafka Docs",
         "snippet": "Official documentation.", "published_at": None},
        {"url": "https://example.com/post", "title": "Post", "snippet": "..."},
        {"not_a_url": True},
    ])
    calls = _patch_cli(monkeypatch, [_FakeProcess(_envelope(results_json))])
    results = asyncio.run(cli_web_search("kafka documentation"))
    assert len(results) == 2  # malformed entry dropped
    assert results[0]["url"] == "https://kafka.apache.org/documentation/"
    assert "WebSearch" in calls[0]         # the CLI's search tool is enabled
    assert "6" in calls[0]                 # multi-turn budget for searching


def test_cli_web_search_salvages_prose_wrapped_json(monkeypatch) -> None:
    from pipeline.providers.claude_cli_adapter import cli_web_search

    raw = 'Here are the results:\n[{"url": "https://a.io", "title": "A", "snippet": "s"}]\nHope this helps!'
    _patch_cli(monkeypatch, [_FakeProcess(_envelope(raw))])
    results = asyncio.run(cli_web_search("q"))
    assert results == [{"url": "https://a.io", "title": "A", "snippet": "s"}]


def test_cli_web_search_returns_empty_on_garbage(monkeypatch) -> None:
    from pipeline.providers.claude_cli_adapter import cli_web_search

    _patch_cli(monkeypatch, [_FakeProcess(_envelope("no json here at all"))])
    assert asyncio.run(cli_web_search("q")) == []


def test_multi_search_falls_back_to_cli(monkeypatch) -> None:
    from pipeline.workers import search_worker

    for var in ("TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY", "EXA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(claude_cli_adapter, "claude_cli_available", lambda: True)

    async def fake_cli_search(query, include_domains=None):
        return [search_worker.SearchResult(
            url="https://kafka.apache.org/", title="Kafka", snippet="docs",
        )]

    monkeypatch.setattr(search_worker, "_claude_cli_search", fake_cli_search)
    results = asyncio.run(search_worker.multi_search(["kafka"]))
    assert [r.url for r in results] == ["https://kafka.apache.org/"]

    # No keys AND no CLI → empty, the pre-existing degraded mode.
    monkeypatch.setattr(claude_cli_adapter, "claude_cli_available", lambda: False)
    assert asyncio.run(search_worker.multi_search(["kafka"])) == []


def test_openai_stages_honor_subscription_pin(monkeypatch) -> None:
    """User picked claude-cli → verification must NOT bill a configured
    OpenAI key. (Bind the real function at import time; the conftest
    hermetic fixture replaces the module attribute, not this reference.)"""
    import main as main_module
    from pipeline.providers.claude_cli_adapter import ClaudeCLIOpenAIFacade
    from pipeline.schemas.models import ArticleRequest

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(main_module, "claude_cli_available", lambda: True)
    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: "claude")

    pinned = real_openai_client(ArticleRequest(topic="x", llm_provider="claude-cli"))
    assert isinstance(pinned, ClaudeCLIOpenAIFacade)
    # Without the pin, the OpenAI key still wins (API users unchanged).
    unpinned = real_openai_client(ArticleRequest(topic="x"))
    assert not isinstance(unpinned, ClaudeCLIOpenAIFacade)


def test_resolve_provider_falls_back_to_cli_without_keys(monkeypatch) -> None:
    import main as main_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr(main_module, "claude_cli_available", lambda: True)
    assert main_module._resolve_provider("auto") == "claude-cli"
    # API keys still win in auto mode.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert main_module._resolve_provider("auto") == "anthropic"
    # Explicit pin beats keys.
    assert main_module._resolve_provider("claude-cli") == "claude-cli"
    # Pin without the binary degrades to auto.
    monkeypatch.setattr(main_module, "claude_cli_available", lambda: False)
    assert main_module._resolve_provider("claude-cli") == "anthropic"