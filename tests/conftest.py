import pytest


@pytest.fixture(autouse=True)
def _hermetic_llm_clients(monkeypatch):
    """Force generate_article onto the mock clients for EVERY test.

    api/server.py runs load_dotenv(override=True) at import, so once
    test_api_server has run, real API keys leak into os.environ for the rest
    of the process. Without this guard, any generate_article test that misses
    a stub for a pipeline stage silently makes REAL paid API calls (this
    happened: the suite was burning Anthropic credits until the balance ran
    out and the tests started failing with 400s). Tests must be hermetic no
    matter what keys are present."""
    import main

    monkeypatch.setattr(
        main, "_anthropic_client", lambda request: main.MockAnthropicClient(request)
    )
    monkeypatch.setattr(
        main, "_openai_client", lambda request: main.MockOpenAIClient(request)
    )

    # api/server.py does `from main import _anthropic_client`, which binds
    # its OWN reference at import time: patching main alone leaves the
    # server calling the real provider. Every API test that forgot a local
    # mock fixture has been making live billed calls through this hole, so
    # the guard has to cover the server module too.
    try:
        from api import server
    except Exception:  # server optional for pure-worker test runs
        return
    monkeypatch.setattr(
        server, "_anthropic_client",
        lambda request: main.MockAnthropicClient(request), raising=False,
    )
    if hasattr(server, "_openai_client"):
        monkeypatch.setattr(
            server, "_openai_client",
            lambda request: main.MockOpenAIClient(request), raising=False,
        )


@pytest.fixture(autouse=True)
def _hermetic_provider_environment(monkeypatch):
    """Provider resolution must not depend on the HOST machine's state.

    Two leaks bit us: (1) server.py's load_dotenv pulls the developer's
    LLM_PROVIDER preference (e.g. claude-cli) into os.environ mid-suite,
    flipping _resolve_provider outcomes based on test ORDER; (2) whether
    the Claude Code CLI happens to be installed on the dev machine changed
    auto-fallback results. Pin both: no provider preference, no CLI.
    Tests that exercise the CLI paths re-patch _find_cli themselves."""
    from pipeline.providers import claude_cli_adapter

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    # The user's saved model choices (settings UI → .env → load_dotenv →
    # os.environ) must not steer test assertions about default models.
    for var in (
        "ANTHROPIC_STRONG_MODEL", "ANTHROPIC_LIGHT_MODEL",
        "OPENAI_STRONG_MODEL", "OPENAI_LIGHT_MODEL", "CLAUDE_CLI_MODEL",
        "LLM_CLI", "CLI_FORCE_MODEL", "CLI_STRONG_MODEL", "CLI_LIGHT_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: None)


@pytest.fixture(autouse=True)
def _stub_official_source_resolution(monkeypatch):
    """generate_article resolves official-doc domains with an LLM call.
    Stub it to the static seed map for every test — deterministic and free."""
    import main
    from pipeline.workers.source_resolver import static_official_sources

    async def _static_only(topic, extra_context, client, preset="balanced"):
        return static_official_sources(topic)

    monkeypatch.setattr(main, "resolve_official_sources", _static_only)
