"""Tests for the OpenAI→Anthropic adapter's model mapping and token budgets."""
from pipeline.providers.openai_adapter import (
    _completion_kwargs,
    _map_model,
    _REASONING_HEADROOM_TOKENS,
)


def test_map_model_tiers(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_STRONG_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_LIGHT_MODEL", raising=False)
    strong = _map_model("claude-sonnet-4-6")
    light = _map_model("claude-haiku-4-5-20251001")
    assert strong != light
    assert _map_model("claude-opus-4-8") == strong  # opus → strong tier


def test_map_model_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_STRONG_MODEL", "my-strong")
    monkeypatch.setenv("OPENAI_LIGHT_MODEL", "my-light")
    assert _map_model("claude-sonnet-4-6") == "my-strong"
    assert _map_model("claude-haiku-4-5-20251001") == "my-light"


def test_reasoning_models_get_headroom_and_effort(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    kw = _completion_kwargs("gpt-5.4", 1024)
    # Reasoning tokens bill against the completion budget — visible-output
    # sizing from callers must be padded or small calls come back empty.
    assert kw["max_completion_tokens"] == 1024 + _REASONING_HEADROOM_TOKENS
    assert kw["reasoning_effort"] == "low"

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "none")
    assert _completion_kwargs("o3-mini", 512)["reasoning_effort"] == "none"


def test_tool_calls_omit_reasoning_effort(monkeypatch) -> None:
    """gpt-5.4 rejects reasoning_effort + function tools on chat completions
    (400: 'use /v1/responses instead') — tool calls must not send the knob."""
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    kw = _completion_kwargs("gpt-5.4", 1024, with_tools=True)
    assert "reasoning_effort" not in kw
    assert kw["max_completion_tokens"] == 1024 + _REASONING_HEADROOM_TOKENS


def test_non_reasoning_models_get_plain_budget() -> None:
    kw = _completion_kwargs("gpt-4.1", 2048)
    assert kw == {"max_completion_tokens": 2048}
