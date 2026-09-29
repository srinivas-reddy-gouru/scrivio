"""Every expensive request is admitted, and every provider call ends (F06).

The follow-up review found the earlier verification narrower than the
claim. The gates covered article jobs and background resume work. Nine
routes that reach a provider had none, the topic classifier ran before
the article gate, and the timeout described as a limit on a call was a
limit on each wait inside one attempt.

No provider is called here. The slow and failing providers are small
servers on loopback, reached by the real SDK clients.
"""
import ast
import asyncio
import http.server
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute

from api import limits, server
from pipeline.providers import claude_cli_adapter, clients

SOURCE = Path(server.__file__).read_text(encoding="utf-8")
REACHES = {"_anthropic_client", "_openai_client", "_client_for_session",
           "_openai_audio_client", "_fetch_job_description", "generate_article"}
# Asks whether a client exists and builds nothing that is used.
ONLY_ASKS = {("GET", "/speak/voices")}


def routes_that_reach_a_provider() -> set[tuple[str, str]]:
    """Found by reading the server: a route whose handler, or anything
    that handler calls in the same file, names a way out."""
    tree = ast.parse(SOURCE)
    functions = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def named(node) -> set[str]:
        return ({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)})

    def reaches(name: str, seen: set[str]) -> bool:
        if name in seen or name not in functions:
            return False
        seen.add(name)
        used = named(functions[name])
        return bool(used & REACHES) or any(reaches(n, seen) for n in used & set(functions))

    found = set()
    for route in server.app.routes:
        if isinstance(route, APIRoute) and reaches(route.endpoint.__name__, set()):
            found |= {(method, route.path) for method in route.methods}
    return found - ONLY_ASKS


def gated(route: APIRoute) -> bool:
    return any(d.call is server.calls_a_provider for d in route.dependant.dependencies)


def test_the_reading_finds_the_routes_it_should():
    found = routes_that_reach_a_provider()

    assert {("POST", "/generate"), ("POST", "/clarify"), ("POST", "/interviews"),
            ("POST", "/interviews/{session_id}/answers"), ("POST", "/job-profiles"),
            ("POST", "/resumes/{resume_id}/request-edit"),
            ("POST", "/resumes/{resume_id}/advise"),
            ("POST", "/speak"), ("POST", "/transcribe")} <= found


def test_every_route_that_can_reach_a_provider_is_behind_the_gate():
    """A route added later that calls a model and forgets the gate fails
    here, by name."""
    reaching = routes_that_reach_a_provider()
    ungated = sorted(
        (method, route.path)
        for route in server.app.routes if isinstance(route, APIRoute)
        for method in route.methods
        if (method, route.path) in reaching and not gated(route))

    assert ungated == []


# ── Admission, with requests that are really in flight ────────────────

@pytest.fixture
def gate(monkeypatch):
    fresh = limits.Gate("request(s) that call a provider", 2)
    monkeypatch.setattr(server, "CALL_GATE", fresh)
    return fresh


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://testserver")


@pytest.fixture
def slow_classifier(monkeypatch):
    """The topic classifier, taking its time, and counting its calls."""
    state = {"calls": 0, "release": None}

    async def classify(topic, extra_context, model_client, preset="balanced"):
        state["calls"] += 1
        await state["release"].wait()
        return "narrow"

    monkeypatch.setattr(server, "classify_topic_breadth", classify)
    return state


async def until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


def test_requests_beyond_the_limit_are_refused_and_the_limit_comes_back(gate, slow_classifier):
    async def scenario():
        slow_classifier["release"] = asyncio.Event()
        async with client() as http:
            held = [asyncio.create_task(http.post("/clarify", json={"topic": f"topic {i}"}))
                    for i in range(2)]
            await until(lambda: gate.running == 2)

            refused = await http.post("/clarify", json={"topic": "one too many"})

            slow_classifier["release"].set()
            answered = await asyncio.gather(*held)
            await until(lambda: gate.running == 0)
            afterwards = await http.post("/clarify", json={"topic": "afterwards"})
        return refused, answered, afterwards

    refused, answered, afterwards = asyncio.run(scenario())

    assert refused.status_code == 429
    assert refused.headers["retry-after"]
    assert "already running" in refused.json()["detail"]
    assert [r.status_code for r in answered] == [200, 200]
    assert afterwards.status_code == 200
    assert slow_classifier["calls"] == 3, "the refused request never reached the classifier"


def test_the_classifier_behind_generate_cannot_be_reached_past_the_limit(
        gate, slow_classifier, monkeypatch):
    """The reviewed case. POST /generate classified the topic before it
    came to the article gate, so the article limit did not limit it."""
    async def scenario():
        slow_classifier["release"] = asyncio.Event()
        async with client() as http:
            held = [asyncio.create_task(http.post("/generate", json={"topic": f"databases {i}"}))
                    for i in range(2)]
            await until(lambda: slow_classifier["calls"] == 2)

            refused = await http.post("/generate", json={"topic": "databases again"})

            for task in held:
                task.cancel()
            await asyncio.gather(*held, return_exceptions=True)
        return refused

    refused = asyncio.run(scenario())

    assert refused.status_code == 429
    assert slow_classifier["calls"] == 2


@pytest.mark.parametrize("path, body", [
    ("/clarify", {"topic": "databases"}),
    ("/interviews", {"topic": "Kafka", "num_questions": 3}),
    ("/job-profiles", {"role_title": "Engineer", "job_description": "x" * 300,
                       "resume_text": "y" * 300}),
    ("/speak", {"text": "hello"}),
    ("/transcribe", {"audio_b64": "AAAA", "mime_type": "audio/webm"}),
    ("/resumes/20260101-000000-abc123/advise", {"question": "how do I improve this?"}),
    ("/resumes/20260101-000000-abc123/request-edit", {"instruction": "tighten it"}),
    ("/interviews/some-session/answers", {"question_id": "q1", "answer": "an answer"}),
])
def test_no_other_endpoint_is_a_way_round(gate, path, body):
    """With the gate full, each of them is refused before it does
    anything, including before it looks up whether the record exists."""
    held = [gate.enter(), gate.enter()]

    async def scenario():
        async with client() as http:
            return await http.post(path, json=body)

    refused = asyncio.run(scenario())
    for slot in held:
        slot.leave()

    assert refused.status_code == 429, refused.text


def test_a_request_that_is_abandoned_gives_its_place_back(gate, slow_classifier):
    async def scenario():
        slow_classifier["release"] = asyncio.Event()
        async with client() as http:
            going = asyncio.create_task(http.post("/clarify", json={"topic": "databases"}))
            await until(lambda: gate.running == 1)
            going.cancel()
            await asyncio.gather(going, return_exceptions=True)
            await until(lambda: gate.running == 0)

    asyncio.run(scenario())

    assert gate.running == 0


def test_a_request_that_fails_gives_its_place_back(gate, monkeypatch):
    async def classify(*_args, **_kwargs):
        raise RuntimeError("the provider fell over")

    monkeypatch.setattr(server, "classify_topic_breadth", classify)

    async def scenario():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
                base_url="http://testserver") as http:
            return await http.post("/clarify", json={"topic": "databases"})

    failed = asyncio.run(scenario())

    assert failed.status_code == 500
    assert gate.running == 0


def test_reading_is_never_refused_for_being_busy(gate):
    held = [gate.enter(), gate.enter()]

    async def scenario():
        async with client() as http:
            return [await http.get(path) for path in
                    ("/health", "/resumes", "/interviews", "/articles", "/mode")]

    answers = asyncio.run(scenario())
    for slot in held:
        slot.leave()

    assert [a.status_code for a in answers] == [200] * 5


def test_diagnostics_reports_the_gate(gate):
    held = gate.enter()

    async def scenario():
        async with client() as http:
            return (await http.get("/diagnostics")).json()

    seen = asyncio.run(scenario())
    held.leave()

    assert seen["provider_calls"] == {"running": 1, "limit": 2}


# ── The deadline, against providers that misbehave ────────────────────

class Provider:
    """A provider on loopback. `behaviour` is what it does with a request:
    "drip" sends a response one byte at a time, each well inside the
    per-wait limit, and never finishes. "fail" answers 500 at once."""

    def __init__(self, behaviour: str):
        self.requests = 0
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                provider.requests += 1
                self.rfile.read(int(self.headers.get("content-length") or 0))
                if behaviour == "fail":
                    body = b'{"error": {"type": "api_error", "message": "unavailable"}}'
                    self.send_response(500)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", "100000")
                self.end_headers()
                try:
                    for _ in range(600):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider(request, monkeypatch):
    made = Provider(request.param)
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-invented-for-a-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-invented-for-a-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", made.address)
    monkeypatch.setenv("OPENAI_BASE_URL", made.address)
    yield made
    made.close()


def ask_anthropic():
    return clients.anthropic_client().messages.create(
        model="claude-sonnet-4-6", max_tokens=16,
        messages=[{"role": "user", "content": "hello"}])


def ask_openai():
    return clients.openai_client().chat.completions.create(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("provider", ["drip"], indirect=True)
@pytest.mark.parametrize("ask", [ask_anthropic, ask_openai], ids=["anthropic", "openai"])
def test_a_response_that_never_finishes_arriving_ends_at_the_deadline(
        provider, ask, monkeypatch):
    """Each byte arrives inside the limit on a wait, so that limit is
    never reached. This is the case it could not end."""
    monkeypatch.setattr(clients, "PROVIDER_SECONDS", 5.0)
    monkeypatch.setattr(clients, "PROVIDER_DEADLINE_SECONDS", 1.5)
    started = time.monotonic()

    with pytest.raises(clients.ProviderTimedOut, match="1.5 seconds, counting retries"):
        asyncio.run(ask())

    assert time.monotonic() - started < 4


@pytest.mark.parametrize("provider", ["fail"], indirect=True)
@pytest.mark.parametrize("ask", [ask_anthropic, ask_openai], ids=["anthropic", "openai"])
def test_retries_are_counted_inside_the_deadline_and_not_added_to_it(
        provider, ask, monkeypatch):
    monkeypatch.setattr(clients, "PROVIDER_DEADLINE_SECONDS", 1.0)
    started = time.monotonic()

    with pytest.raises(Exception) as ended:
        asyncio.run(ask())

    assert time.monotonic() - started < 3
    assert 1 <= provider.requests <= clients.PROVIDER_RETRIES + 1
    # Either the retries ran out inside the deadline, or the deadline
    # cut them short. Both are an end, and both are inside the limit.
    assert isinstance(ended.value, clients.ProviderTimedOut) or provider.requests == 3


def test_the_deadline_is_on_every_call_made_through_the_client(monkeypatch):
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-invented-for-a-test")
    built = clients.openai_client()

    for namespace in (built.chat.completions, built.beta.chat.completions,
                      built.audio.speech, built.audio.transcriptions):
        assert isinstance(namespace, clients.Deadlined)
    assert built.max_retries == clients.PROVIDER_RETRIES
    assert built.timeout.read == clients.PROVIDER_SECONDS


def test_what_the_user_is_told_names_no_key_and_no_address(monkeypatch):
    async def never():
        await asyncio.sleep(30)

    with pytest.raises(clients.ProviderTimedOut) as ended:
        asyncio.run(clients.within_deadline(never(), "OpenAI", 0.05))

    said = str(ended.value)
    assert "OpenAI did not answer within 0.05 seconds" in said
    assert "sk-" not in said and "http" not in said


def test_a_call_to_the_assistant_has_a_limit_over_all_its_runs(monkeypatch):
    """One run has its own timeout. A call can be more than one run,
    when the answer must be JSON and is not, and had no limit over all."""
    runs = []

    async def run(self, model, prompt, tools="", max_turns=1):
        runs.append(time.monotonic())
        await asyncio.sleep(0.4)
        return "this is not JSON"

    monkeypatch.setattr(claude_cli_adapter._CLIMessages, "_run_cli", run)
    monkeypatch.setattr(claude_cli_adapter, "CALL_DEADLINE_S", 0.6)
    started = time.monotonic()

    with pytest.raises(clients.ProviderTimedOut, match="command-line assistant"):
        asyncio.run(claude_cli_adapter.ClaudeCLIAdapter().messages.create(
            model="claude-sonnet-4-6", max_tokens=100,
            tools=[{"name": "submit", "input_schema": {"type": "object"}}],
            tool_choice={"type": "tool", "name": "submit"},
            messages=[{"role": "user", "content": "hello"}]))

    assert time.monotonic() - started < 2
    assert len(runs) == 2, "it was asked again once, and the second run was cut short"
