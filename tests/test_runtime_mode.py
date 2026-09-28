"""Demo mode is explicit, and real mode never serves mock output (R09).

The rest of the suite runs on mock clients because conftest swaps the
client factories, which is a decision made in the test process. This
file is about what the REAL factories decide, so it keeps references to
them from before conftest replaces them.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from api import server
from main import _anthropic_client as REAL_WRITER
from main import _openai_client as REAL_VERIFIER
from pipeline.providers import claude_cli_adapter
from pipeline.runtime_mode import ProviderUnavailable
from pipeline.schemas.models import ArticleRequest

REQUEST = ArticleRequest(topic="Kafka rebalancing")
KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


@pytest.fixture
def real(monkeypatch):
    """Real factories, real mode, and a machine with nothing configured."""
    monkeypatch.setattr(main, "_anthropic_client", REAL_WRITER)
    monkeypatch.setattr(main, "_openai_client", REAL_VERIFIER)
    monkeypatch.setattr(server, "_anthropic_client", REAL_WRITER)
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _is_mock(client) -> bool:
    return "Mock" in type(client).__name__


# ── Real mode ────────────────────────────────────────────────────────

def test_with_nothing_configured_writing_fails_instead_of_faking(real):
    with pytest.raises(ProviderUnavailable) as refused:
        REAL_WRITER(REQUEST)

    message = str(refused.value)
    assert "Settings" in message and "demo" in message.lower(), \
        "the error must say how to fix it and that demo mode exists"


def test_with_nothing_configured_fact_checking_fails_instead_of_faking(real):
    with pytest.raises(ProviderUnavailable):
        REAL_VERIFIER(REQUEST)


def test_an_anthropic_only_setup_gets_real_fact_checking(real):
    """The reported case: writing on a real Anthropic key while the
    fact-check quietly ran on the mock, so every claim 'passed'."""
    real.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")

    writer, verifier = REAL_WRITER(REQUEST), REAL_VERIFIER(REQUEST)

    assert not _is_mock(writer) and not _is_mock(verifier)
    assert type(verifier).__name__ == "AnthropicOpenAIFacade"


def test_an_openai_only_setup_runs_both_on_openai(real):
    real.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    writer, verifier = REAL_WRITER(REQUEST), REAL_VERIFIER(REQUEST)

    assert type(writer).__name__ == "OpenAIAnthropicAdapter"
    assert type(verifier).__name__ == "AsyncOpenAI"


def test_both_keys_keep_writing_on_anthropic_and_checking_on_openai(real):
    real.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    real.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    assert type(REAL_WRITER(REQUEST)).__name__ == "AsyncAnthropic"
    assert type(REAL_VERIFIER(REQUEST)).__name__ == "AsyncOpenAI"


def test_a_subscription_cli_runs_both_without_touching_a_key(real):
    real.setattr(claude_cli_adapter, "_find_cli", lambda: "/usr/local/bin/claude")
    real.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    real.setenv("LLM_PROVIDER", "claude-cli")

    assert type(REAL_WRITER(REQUEST)).__name__ == "ClaudeCLIAdapter"
    assert type(REAL_VERIFIER(REQUEST)).__name__ == "ClaudeCLIOpenAIFacade"


def test_the_stage_roadmap_never_names_a_mock_in_real_mode(real):
    real.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")

    stages = main._pipeline_models(REQUEST)["stages"]

    assert not [s for s, model in stages.items() if "mock" in model.lower()]


# ── A CLI that is installed is not a CLI that is signed in ───────────

class _Process:
    def __init__(self, code: int, stderr: bytes):
        self.returncode, self._stderr = code, stderr

    async def communicate(self, _input):
        return b"", self._stderr

    def kill(self):
        pass


@pytest.mark.parametrize("stderr", [
    b"Invalid API key \xc2\xb7 Please run /login",
    b"Error: not logged in. Run `claude login` to authenticate.",
    b"401 Unauthorized: authentication required",
])
def test_an_installed_but_signed_out_cli_says_so(real, stderr):
    real.setattr(claude_cli_adapter, "_find_cli", lambda: "/usr/local/bin/claude")

    async def spawn(*argv, **kwargs):
        return _Process(1, stderr)
    real.setattr(asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(ProviderUnavailable) as refused:
        asyncio.run(claude_cli_adapter.ClaudeCLIAdapter().messages.create(
            model="sonnet", messages=[{"role": "user", "content": "hello"}]))

    assert "signed in" in str(refused.value)
    assert claude_cli_adapter.cli_status()["state"] == "installed, not signed in"


def test_an_ordinary_cli_failure_is_not_mistaken_for_a_sign_in_problem(real):
    real.setattr(claude_cli_adapter, "_find_cli", lambda: "/usr/local/bin/claude")

    async def spawn(*argv, **kwargs):
        return _Process(2, b"rate limit reached, retry later")
    real.setattr(asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(claude_cli_adapter.ClaudeCLIError) as failed:
        asyncio.run(claude_cli_adapter.ClaudeCLIAdapter().messages.create(
            model="sonnet", messages=[{"role": "user", "content": "hello"}]))

    assert not isinstance(failed.value, ProviderUnavailable)


# ── The fact-checking facade over Anthropic ──────────────────────────

def test_the_anthropic_facade_returns_what_the_verifier_expects():
    from pydantic import BaseModel

    from pipeline.providers.anthropic_facade import AnthropicOpenAIFacade

    class Verdict(BaseModel):
        supported: bool
        reason: str

    calls = []

    class FakeAnthropic:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("tools"):
                return SimpleNamespace(content=[SimpleNamespace(
                    type="tool_use", input={"supported": True, "reason": "stated in source"})])
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="plain answer")])

    facade = AnthropicOpenAIFacade(FakeAnthropic())
    messages = [{"role": "system", "content": "Judge."}, {"role": "user", "content": "Claim."}]

    parsed = asyncio.run(facade.beta.chat.completions.parse(
        model="gpt-4o-mini", messages=messages, response_format=Verdict))
    text = asyncio.run(facade.chat.completions.create(model="gpt-4o-mini", messages=messages))

    assert parsed.choices[0].message.parsed == Verdict(supported=True, reason="stated in source")
    assert text.choices[0].message.content == "plain answer"
    assert calls[0]["system"] == "Judge." and calls[0]["tool_choice"]["type"] == "tool"


# ── Through the API ──────────────────────────────────────────────────

def test_the_api_reports_a_missing_provider_instead_of_a_mock_review(real):
    client = TestClient(server.app)

    created = client.post("/resumes", json={"resume_text": "Sam Okafor\nEngineer\n" * 30})
    doc = client.get(f"/resumes/{created.json()['resume_id']}").json()

    assert doc["status"] == "error", "analysis must not finish on mock output"
    assert doc["review"] is None
    assert "provider" in doc["error"].lower()


def test_starting_an_article_with_no_provider_is_refused_up_front(real):
    r = TestClient(server.app).post("/generate", json={"topic": "Kafka rebalancing basics"})

    assert r.status_code == 503
    assert "provider" in r.json()["detail"].lower()


def test_starting_an_interview_with_no_provider_is_refused(real):
    r = TestClient(server.app).post("/interviews", json={"topic": "Kafka"})

    assert r.status_code == 503


def test_the_mode_endpoint_describes_real_mode_accurately(real):
    real.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")

    mode = TestClient(server.app).get("/mode").json()

    assert mode["demo"] is False
    assert mode["writing"] == "anthropic"
    assert mode["fact_checking"] == "anthropic"
    assert "sk-ant" not in str(mode), "no credential material in a status response"


def test_the_mode_endpoint_says_when_nothing_can_run(real):
    mode = TestClient(server.app).get("/mode").json()

    assert mode["writing"] == "none" and mode["fact_checking"] == "none"
    assert mode["ready"] is False and mode["problem"]


# ── Demo mode ────────────────────────────────────────────────────────

@pytest.fixture
def demo(real):
    real.setenv("SCRIVIO_DEMO", "1")
    return real


def test_demo_mode_uses_canned_clients_even_when_keys_exist(demo):
    """Demo means no real calls. A key that happens to be set must not
    turn a demonstration into a bill."""
    demo.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    demo.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")

    assert _is_mock(REAL_WRITER(REQUEST)) and _is_mock(REAL_VERIFIER(REQUEST))


def test_demo_mode_is_reported_as_demo(demo):
    response = TestClient(server.app).get("/mode")

    assert response.json()["demo"] is True
    assert response.headers["x-scrivio-mode"] == "demo"


def test_demo_output_says_what_it_is(demo):
    client = TestClient(server.app)
    rid = client.post("/resumes", json={
        "resume_text": "Sam Okafor\nEngineer\n" * 30,
        "jd_text": "Senior engineer. Requirements: Python, Kafka."}).json()["resume_id"]
    client.post(f"/resumes/{rid}/tailor")
    doc = client.get(f"/resumes/{rid}").json()

    assert doc["review"]["summary"].startswith("DEMO")
    assert doc["tailored"]["warnings"][0].startswith("DEMO")
    assert "not an analysis of your resume" in doc["tailored"]["warnings"][0]


def test_demo_work_is_kept_apart_from_real_work(demo, tmp_path):
    """Two directories, so a canned resume review can never turn up in
    the history of someone's real job search."""
    demo.setenv("ARTICLE_OUTPUT_DIR", str(tmp_path / "out"))
    real_root = server.output_root_for(demo=False)
    demo_root = server.output_root_for(demo=True)

    assert demo_root != real_root
    assert real_root not in demo_root.parents or demo_root.name == "demo-mode"
    assert not list(real_root.glob("resumes/*.json")) if real_root.exists() else True


def test_demo_mode_does_not_write_to_the_stage_cache(demo, tmp_path):
    from pipeline.cache import StageCache

    cache = StageCache()
    cache.set("planning", {"canned": True}, "Kafka rebalancing")

    assert cache.get("planning", "Kafka rebalancing") is None
