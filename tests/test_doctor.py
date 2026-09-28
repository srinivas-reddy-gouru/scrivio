"""The setup check (review item R17): accurate, and silent about secrets."""
import io

import pytest

from api import doctor


@pytest.fixture
def clean(monkeypatch, tmp_path):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY",
                 "BRAVE_SEARCH_API_KEY", "EXA_API_KEY", "SCRIVIO_DEMO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARTICLE_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("SCRIVIO_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    (tmp_path / "web" / "dist").mkdir(parents=True)
    (tmp_path / "web" / "dist" / "index.html").write_text("<html></html>")
    return monkeypatch, tmp_path


def run() -> tuple[int, str]:
    out = io.StringIO()
    report = doctor.Report()
    for check in (doctor.check_python, doctor.check_packages, doctor.check_interface,
                  doctor.check_provider, doctor.check_extras, doctor.check_storage):
        check(report)
    report.show(out)
    return (1 if report.failed else 0), out.getvalue()


def test_no_provider_is_a_problem_with_a_remedy(clean):
    code, text = run()

    assert code == 1
    assert "Provider" in text and "none configured" in text
    assert "SCRIVIO_DEMO=1" in text, "it says how to look around without one"


def test_a_configured_provider_is_ready_and_the_key_is_never_printed(clean):
    monkeypatch, _ = clean
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key-VALUE")

    code, text = run()

    assert code == 0
    assert "Anthropic" in text
    assert "sk-ant" not in text and "VALUE" not in text


def test_a_missing_interface_build_is_a_problem_with_the_exact_command(clean):
    monkeypatch, root = clean
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    (root / "web" / "dist" / "index.html").unlink()

    code, text = run()

    assert code == 1
    assert "not built" in text and "classic" in text
    assert "npm ci && npm run build" in text


def test_demo_mode_is_reported_as_what_it_is(clean):
    monkeypatch, _ = clean
    monkeypatch.setenv("SCRIVIO_DEMO", "1")

    code, text = run()

    assert code == 0
    assert "demo mode" in text and "no model is called" in text


def test_an_installed_cli_is_not_reported_as_a_working_one(clean):
    """The binary being on disk says nothing about the account behind it."""
    from pipeline.providers import claude_cli_adapter

    monkeypatch, _ = clean
    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli_adapter, "_last_call", {"state": "unknown", "at": None})

    _code, text = run()

    assert "installed, not yet used" in text
    assert "installed is not the same as signed in" in text


def test_a_session_key_others_can_read_is_a_problem(clean):
    import os

    monkeypatch, root = clean
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    state = root / "state"
    state.mkdir()
    (state / "session.key").write_bytes(b"k" * 48)
    os.chmod(state / "session.key", 0o644)
    monkeypatch.setenv("SCRIVIO_STATE_DIR", str(state))

    code, text = run()

    assert code == 1 and "readable by others" in text


def test_the_check_calls_no_provider(clean, monkeypatch):
    import asyncio
    import subprocess

    monkeypatch_, _ = clean
    monkeypatch_.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("the setup check made a network call")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)

    code, _text = run()

    assert code == 0


def test_the_fallback_to_the_older_interface_is_reported_by_the_api(monkeypatch):
    from fastapi.testclient import TestClient

    from api import boundary, server

    monkeypatch.setattr(boundary, "modern_interface_at_root", False)

    assert TestClient(server.app).get("/mode").json()["interface"] == "classic"
