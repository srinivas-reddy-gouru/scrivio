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


@pytest.fixture(autouse=True)
def _stub_official_source_resolution(monkeypatch):
    """generate_article resolves official-doc domains with an LLM call.
    Stub it to the static seed map for every test — deterministic and free."""
    import main
    from pipeline.workers.source_resolver import static_official_sources

    async def _static_only(topic, extra_context, client, preset="balanced"):
        return static_official_sources(topic)

    monkeypatch.setattr(main, "resolve_official_sources", _static_only)
