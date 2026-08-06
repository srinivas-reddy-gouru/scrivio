"""User model selection: tier overrides + CLI passthrough + settings surface.

Presets choose which TIER a stage uses; ANTHROPIC_STRONG_MODEL /
ANTHROPIC_LIGHT_MODEL choose what model each tier IS, and those choices
must flow through both the API path (get_model) and the Claude CLI path
(_model_alias passes unknown ids verbatim to `claude --model`).
"""
from pathlib import Path

from fastapi.testclient import TestClient

from api import server
from pipeline.model_config import get_model
from pipeline.providers.claude_cli_adapter import _model_alias


# ── Tier overrides in get_model ──────────────────────────────────────

def test_strong_tier_override_applies_to_all_strong_stages(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_STRONG_MODEL", "claude-opus-4-7")
    monkeypatch.delenv("ANTHROPIC_LIGHT_MODEL", raising=False)
    assert get_model("drafting", "balanced") == "claude-opus-4-7"
    assert get_model("evaluator", "balanced") == "claude-opus-4-7"
    # Light-tier stages are untouched by the strong override.
    assert "haiku" in get_model("relevance", "balanced")


def test_light_tier_override(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_STRONG_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_LIGHT_MODEL", "claude-haiku-99")
    assert get_model("relevance", "balanced") == "claude-haiku-99"
    assert "sonnet" in get_model("drafting", "balanced")


def test_no_override_returns_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_STRONG_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_LIGHT_MODEL", raising=False)
    assert get_model("drafting", "balanced") == "claude-sonnet-4-6"


# ── CLI model argument resolution ────────────────────────────────────

def test_cli_alias_families_and_custom_passthrough(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CLI_MODEL", raising=False)
    assert _model_alias("claude-sonnet-4-6") == "sonnet"
    assert _model_alias("claude-haiku-4-5-20251001") == "haiku"
    assert _model_alias("claude-opus-4-7") == "opus"
    # A custom id from a tier override passes through VERBATIM — never
    # silently rewritten to sonnet.
    assert _model_alias("claude-fable-5") == "claude-fable-5"
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "haiku")
    assert _model_alias("claude-fable-5") == "haiku"  # global hammer wins


# ── Settings surface ─────────────────────────────────────────────────

def _isolated_env_file(monkeypatch, tmp_path: Path, content: str = "") -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(server, "_ENV_FILE", env_file)
    return env_file


def test_settings_expose_model_keys_in_plain_text(monkeypatch, tmp_path) -> None:
    _isolated_env_file(
        monkeypatch, tmp_path,
        "ANTHROPIC_STRONG_MODEL=claude-opus-4-7\nANTHROPIC_API_KEY=sk-secret-12345\n",
    )
    monkeypatch.delenv("ANTHROPIC_STRONG_MODEL", raising=False)
    client = TestClient(server.app)
    keys = {k["key"]: k for k in client.get("/settings").json()["keys"]}

    assert keys["ANTHROPIC_STRONG_MODEL"]["plain"] is True
    assert keys["ANTHROPIC_STRONG_MODEL"]["masked_value"] == "claude-opus-4-7"
    assert keys["CLAUDE_CLI_MODEL"]["plain"] is True
    # Secrets stay masked.
    assert keys["ANTHROPIC_API_KEY"]["plain"] is False
    assert "sk-secret" not in keys["ANTHROPIC_API_KEY"]["masked_value"]


def test_settings_patch_sets_and_clears_model_keys(monkeypatch, tmp_path) -> None:
    env_file = _isolated_env_file(monkeypatch, tmp_path)
    monkeypatch.delenv("ANTHROPIC_STRONG_MODEL", raising=False)
    client = TestClient(server.app)

    response = client.patch(
        "/settings",
        json={"updates": {"ANTHROPIC_STRONG_MODEL": "claude-opus-4-7"}},
    )
    assert response.status_code == 200
    assert "ANTHROPIC_STRONG_MODEL=claude-opus-4-7" in env_file.read_text()
    # Hot-reloaded into the process → get_model sees it immediately.
    assert get_model("drafting", "balanced") == "claude-opus-4-7"

    response = client.patch(
        "/settings", json={"updates": {"ANTHROPIC_STRONG_MODEL": ""}}
    )
    assert response.status_code == 200
    assert "ANTHROPIC_STRONG_MODEL" not in env_file.read_text()
    assert get_model("drafting", "balanced") == "claude-sonnet-4-6"
