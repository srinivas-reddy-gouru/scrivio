"""Demo mode reaches nobody, whatever is configured (F01, reopening R09).

The earlier tests of demo mode ran with every key removed, which removes
the condition that matters. A person who tries demo mode on a machine
they already use has keys in their settings file and a command-line
assistant signed in. These tests keep all of that in place, invented,
and watch every way out of the process.

What "reaches nobody" covers: model providers, search providers, search
through a command-line assistant, fetching a page from an address, text
to speech, and transcription. It does not cover what the browser does
by itself, which the server cannot see. The interface turns those off
in demo mode, and tests/browser/test_demo_voice.py checks that.
"""
import asyncio
import base64
import json
import os
import socket
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from api import server
from main import _anthropic_client as REAL_WRITER
from main import _openai_client as REAL_VERIFIER
from pipeline.providers import claude_cli_adapter
from pipeline.schemas.models import JobProfile

INVENTED_KEYS = {
    "ANTHROPIC_API_KEY": "sk-ant-invented-for-a-test",
    "OPENAI_API_KEY": "sk-invented-for-a-test",
    "TAVILY_API_KEY": "tvly-invented-for-a-test",
    "BRAVE_SEARCH_API_KEY": "brave-invented-for-a-test",
    "EXA_API_KEY": "exa-invented-for-a-test",
    "JINA_API_KEY": "jina-invented-for-a-test",
}

RESUME = """Jordan Rivera
Backend Engineer
jordan@example.com | +1 555 010 1234 | Austin, TX

Experience
Software Engineer, Acme Corp
Jan 2021 - Present
- Built Kafka pipelines processing 2M events/day

Education
B.S. in Computer Science, State University, 2015 - 2019

Skills
Python, Kafka
"""
POSTING = ("Senior Backend Engineer. We need someone to run Kafka pipelines on "
           "Kubernetes and work across teams. Requirements: Python, Kafka, Kubernetes. ") * 4


class Spies:
    """Everything that left, or tried to."""

    def __init__(self):
        self.seen: list[tuple[str, str]] = []

    def saw(self, kind: str, what: str) -> None:
        self.seen.append((kind, str(what)))

    def of(self, kind: str) -> list[str]:
        return [what for k, what in self.seen if k == kind]


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """A machine with everything set up: every key, and a command-line
    assistant that is installed and signed in. All of it invented."""
    monkeypatch.setattr(main, "_anthropic_client", REAL_WRITER)
    monkeypatch.setattr(main, "_openai_client", REAL_VERIFIER)
    monkeypatch.setattr(server, "_anthropic_client", REAL_WRITER)
    for name, value in INVENTED_KEYS.items():
        monkeypatch.setenv(name, value)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "cli-was-called"
    cli = bin_dir / "claude"
    cli.write_text(f"#!/bin/sh\necho \"$@\" >> '{called}'\necho '{{\"result\": \"ok\"}}'\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("LLM_CLI", "claude")
    # The suite hides whatever assistant is installed on the machine.
    # This puts one back: the invented one above, and only that one.
    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: str(cli))
    monkeypatch.setattr(claude_cli_adapter, "_last_call",
                        {"state": "ok", "at": "2026-01-01T00:00:00"})
    assert claude_cli_adapter.claude_cli_available()

    spies = Spies()
    spies.cli_was_called = called

    async def no_http(self, request):
        spies.saw("http", request.url.host)
        raise httpx.ConnectError("refused by the test", request=request)

    def no_http_sync(self, request):
        spies.saw("http", request.url.host)
        raise httpx.ConnectError("refused by the test", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_http_sync)

    real_lookup = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1", "testserver"):
            spies.saw("dns", host)
            raise socket.gaierror("refused by the test")
        return real_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)

    real_spawn = asyncio.create_subprocess_exec

    async def spawn(program, *args, **kwargs):
        spies.saw("process", os.path.basename(str(program)))
        return await real_spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    from pipeline.providers import clients
    for name in ("anthropic_client", "openai_client"):
        real = getattr(clients, name)

        def construct(*args, _real=real, _name=name, **kwargs):
            spies.saw("sdk", _name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(clients, name, construct)
    return spies


@pytest.fixture
def demo(configured, monkeypatch):
    monkeypatch.setenv("SCRIVIO_DEMO", "1")
    return configured


def nothing_left(spies: Spies) -> None:
    assert spies.seen == []
    assert not spies.cli_was_called.exists(), spies.cli_was_called.read_text()


# ── The spies work ────────────────────────────────────────────────────
# Without these, "the spies saw nothing" could mean the spies are blind.

def test_in_real_mode_the_same_setup_does_reach_a_search_provider(configured, monkeypatch):
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)
    from pipeline.workers.interviewer_worker import find_real_question_patterns

    asyncio.run(find_real_question_patterns("Kafka", "intermediate"))

    assert "api.tavily.com" in configured.of("http")


def test_in_real_mode_the_same_setup_does_build_an_audio_client(configured, monkeypatch):
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)

    assert server._openai_audio_client() is not None
    assert configured.of("sdk") == ["openai_client"]


# ── Each way out, one at a time ───────────────────────────────────────

def test_demo_research_for_a_topic_interview_searches_nothing(demo):
    from pipeline.workers.interviewer_worker import find_real_question_patterns

    assert asyncio.run(find_real_question_patterns("Kafka", "intermediate")) == []
    nothing_left(demo)


def test_demo_research_for_a_job_interview_searches_nothing(demo):
    from pipeline.workers.job_interviewer_worker import research_job_questions

    profile = JobProfile(
        profile_id="p1", role_title="Backend Engineer", company="Example Payments Inc",
        job_description=POSTING, resume_text=RESUME, created_at="2026-01-01T00:00:00")

    assert asyncio.run(research_job_questions(profile)) == []
    nothing_left(demo)


def test_demo_search_is_empty_whichever_provider_is_asked_for(demo):
    from pipeline.workers.search_worker import multi_search

    for provider in (None, "tavily", "brave", "exa"):
        assert asyncio.run(multi_search(["kafka"], provider=provider)) == []
    nothing_left(demo)


@pytest.mark.parametrize("name", ["search_tavily", "search_brave", "search_exa",
                                  "_claude_cli_search"])
def test_demo_refuses_a_search_provider_called_directly(demo, name):
    """The floor. A new caller that forgets to ask about demo mode is
    refused here, and does not get through."""
    from pipeline import runtime_mode
    from pipeline.workers import search_worker

    with pytest.raises(runtime_mode.DemoRefused):
        asyncio.run(getattr(search_worker, name)("kafka"))
    nothing_left(demo)


def test_demo_refuses_to_build_a_provider_client(demo):
    from pipeline import runtime_mode
    from pipeline.providers import clients

    for build in (clients.anthropic_client, clients.openai_client):
        with pytest.raises(runtime_mode.DemoRefused):
            build()
    assert demo.of("http") == [] and demo.of("process") == []


def test_demo_refuses_to_start_the_command_line_assistant(demo):
    from pipeline import runtime_mode

    adapter = claude_cli_adapter.ClaudeCLIAdapter()
    with pytest.raises(runtime_mode.DemoRefused):
        asyncio.run(adapter.messages.create(
            model="claude-sonnet-4-6", max_tokens=10,
            messages=[{"role": "user", "content": "hello"}]))
    with pytest.raises(runtime_mode.DemoRefused):
        asyncio.run(claude_cli_adapter.cli_web_search("kafka"))
    nothing_left(demo)


def test_demo_refuses_to_fetch_a_page(demo):
    from pipeline import net_guard

    with pytest.raises(net_guard.BlockedFetch, match="[Dd]emo"):
        asyncio.run(net_guard.guarded_get("https://jobs.example.com/posting/1"))
    nothing_left(demo)


def test_demo_has_no_audio_client(demo):
    assert server._openai_audio_client() is None
    nothing_left(demo)


# ── Through the application ───────────────────────────────────────────

def _follow(client, job_id) -> list[dict]:
    events = []
    with client.stream("GET", f"/jobs/{job_id}/stream") as stream:
        for line in stream.iter_lines():
            if line and line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def test_a_demo_article_reaches_nobody(demo):
    # Entered as a context, so that one event loop lasts from the request
    # that starts the run to the stream that follows it. Without that the
    # run is abandoned when the first request's loop closes.
    with TestClient(server.app) as client:
        started = client.post("/generate", json={
            "topic": "How Kafka consumer group rebalancing works",
            "skip_clarification": True, "web_search": True})
        assert started.status_code == 200, started.text
        events = _follow(client, started.json()["job_id"])

    assert events[-1]["type"] == "complete", events[-1]
    assert "stage_completed" in {e["type"] for e in events}
    nothing_left(demo)


def test_a_demo_article_that_asks_for_clarification_reaches_nobody(demo):
    client = TestClient(server.app)

    asked = client.post("/generate", json={"topic": "database"})

    assert asked.status_code == 200, asked.text
    nothing_left(demo)


def test_a_demo_topic_interview_reaches_nobody(demo):
    client = TestClient(server.app)

    created = client.post("/interviews", json={
        "topic": "Kafka consumer groups", "num_questions": 3})
    assert created.status_code == 200, created.text
    session = created.json()
    answered = client.post(f"/interviews/{session['session_id']}/answers", json={
        "question_id": session["questions"][0]["id"],
        "answer": "A consumer group rebalances when its membership changes and "
                  "consumption pauses while partitions are reassigned."})

    assert answered.status_code == 200, answered.text
    nothing_left(demo)


def test_a_demo_job_target_and_its_interview_reach_nobody(demo):
    client = TestClient(server.app)

    profile = client.post("/job-profiles", json={
        "role_title": "Senior Backend Engineer", "company": "Example Payments Inc",
        "job_description": POSTING, "resume_text": RESUME})
    assert profile.status_code == 200, profile.text
    profile_id = profile.json()["profile"]["profile_id"]
    screen = client.post("/interviews", json={
        "mode": "job", "job_profile_id": profile_id, "duration_minutes": 30})

    assert screen.status_code == 200, screen.text
    nothing_left(demo)


def test_demo_voices_are_reported_unavailable_and_say_why(demo):
    client = TestClient(server.app)

    voices = client.get("/speak/voices")
    spoken = client.post("/speak", json={"text": "Tell me about consumer groups."})

    assert voices.json()["available"] is False
    assert spoken.status_code == 503 and "demo" in spoken.json()["detail"].lower()
    nothing_left(demo)


def test_demo_transcription_is_unavailable_and_says_why(demo):
    client = TestClient(server.app)

    heard = client.post("/transcribe", json={
        "audio_b64": base64.b64encode(b"\x1aE\xdf\xa3 not really audio").decode(),
        "mime_type": "audio/webm"})

    assert heard.status_code == 503 and "demo" in heard.json()["detail"].lower()
    nothing_left(demo)


@pytest.mark.parametrize("path, body", [
    ("/resumes", {"resume_text": RESUME, "jd_url": "https://jobs.example.com/posting/1"}),
    ("/job-profiles", {"role_title": "Backend Engineer", "resume_text": RESUME,
                       "jd_url": "https://jobs.example.com/posting/1"}),
])
def test_a_demo_posting_address_is_not_fetched_and_the_page_says_so(demo, path, body):
    client = TestClient(server.app)

    refused = client.post(path, json=body)

    assert refused.status_code == 422, refused.text
    said = refused.json()["detail"]
    assert "demo" in said.lower() and "paste" in said.lower()
    nothing_left(demo)


def test_what_the_demo_says_about_itself_is_now_true(demo):
    """"Nothing is sent anywhere" was on this page while search and speech
    still went out. The words are unchanged. What they describe is not."""
    client = TestClient(server.app)

    mode = client.get("/mode").json()
    data = client.get("/data").json()

    assert mode["demo"] is True and mode["ready"] is True
    assert data["processed_by"]["statement"] == "Nothing is sent anywhere: this is demo mode."
    for shown in (json.dumps(mode), json.dumps(data)):
        assert "invented-for-a-test" not in shown
    nothing_left(demo)
