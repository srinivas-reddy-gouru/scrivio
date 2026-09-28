"""Readiness, diagnostics, request ids, and what failures say (R21)."""
import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from api import jobs, observability, server
from pipeline.schemas.models import ProgressEvent

SECRET = "sk-ant-test-not-a-real-key-VALUE"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)
    jobs.clear_jobs()
    return TestClient(server.app)


# ── Liveness is not readiness ────────────────────────────────────────

def test_with_no_provider_the_process_is_alive_and_not_ready(client):
    alive, ready = client.get("/health"), client.get("/ready")

    assert alive.status_code == 200 and alive.json() == {"ok": True}
    assert ready.status_code == 503
    assert ready.json() == {"ready": False, "checks": {
        "storage": "ok", "provider": "missing", "interface": ready.json()["checks"]["interface"],
        "capacity": "ok"}}


def test_with_a_provider_it_is_ready(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    ready = client.get("/ready")

    assert ready.status_code == 200 and ready.json()["ready"] is True
    assert "sk-ant" not in ready.text and "VALUE" not in ready.text


def test_storage_that_cannot_be_written_makes_it_not_ready(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    blocked = tmp_path / "a-file-not-a-folder"
    blocked.write_text("x")
    monkeypatch.setattr(server, "OUTPUT_ROOT", blocked)

    ready = client.get("/ready")

    assert ready.status_code == 503
    assert ready.json()["checks"]["storage"] == "failing"
    assert client.get("/health").status_code == 200, "and it is still alive"


def test_a_signed_out_cli_makes_it_not_ready(client, monkeypatch):
    from pipeline.providers import claude_cli_adapter

    monkeypatch.setattr(claude_cli_adapter, "_find_cli", lambda: "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli_adapter, "_last_call",
                        {"state": "signed_out", "at": "2026-01-01T00:00:00"})

    assert client.get("/ready").json()["checks"]["provider"] == "signed out"


def test_full_capacity_is_reported_and_does_not_make_it_unready(client, monkeypatch):
    from api import limits

    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    gate = limits.Gate("article generation", 1)
    gate.enter()
    monkeypatch.setattr(server, "ARTICLE_GATE", gate)

    ready = client.get("/ready")

    assert ready.status_code == 200 and ready.json()["checks"]["capacity"] == "full"


def test_readiness_calls_no_provider(client, monkeypatch):
    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    def refuse(*args, **kwargs):
        raise AssertionError("a health check made a provider call")
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)

    for _ in range(5):
        assert client.get("/ready").status_code == 200


def test_readiness_needs_no_session_and_diagnostics_does(client, monkeypatch):
    from api import boundary
    from api.boundary import request_is_authenticated

    monkeypatch.setattr(boundary, "request_is_authenticated",
                        lambda cookies: False)

    assert client.get("/ready").status_code in (200, 503)
    assert client.get("/health").status_code == 200
    assert client.get("/diagnostics").status_code == 401


# ── Request ids ──────────────────────────────────────────────────────

def test_every_response_carries_a_request_id(client):
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]

    assert len(first) >= 8 and first != second


def test_an_id_from_the_caller_is_kept_when_it_looks_like_one(client):
    r = client.get("/health", headers={"X-Request-ID": "trace-0123456789"})

    assert r.headers["x-request-id"] == "trace-0123456789"


@pytest.mark.parametrize("offered", ["short", "has spaces in it", "x" * 200, "a\r\nSet-Cookie: x=1"])
def test_an_id_that_does_not_look_like_one_is_replaced(client, offered):
    r = client.get("/health", headers={"X-Request-ID": offered.replace("\r\n", "")})

    assert r.headers["x-request-id"] != offered
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_log_lines_written_during_a_request_carry_its_id():
    """The middleware on its own, around an application that logs."""
    seen = {}

    async def application(scope, receive, send):
        record = logging.LogRecord("t", logging.INFO, "", 0, "hello", (), None)
        observability.ContextFilter().filter(record)
        seen["during"] = record.request_id
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def nothing():
        return {"type": "http.request", "body": b""}

    async def discard(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/",
             "headers": [(b"x-request-id", b"trace-abcdef0123")]}
    asyncio.run(observability.RequestId(application)(scope, nothing, discard))
    after = logging.LogRecord("t", logging.INFO, "", 0, "later", (), None)
    observability.ContextFilter().filter(after)

    assert seen["during"] == "trace-abcdef0123"
    assert after.request_id == "-", "and it does not leak into what comes next"


# ── What failures say ────────────────────────────────────────────────

@pytest.mark.parametrize("exc, expected", [
    (TimeoutError("read timed out after 180s on /v1/messages"), "took too long"),
    (type("RateLimitError", (Exception,), {})("429 for org-abc123"), "limiting requests"),
    (type("AuthenticationError", (Exception,), {})(f"invalid x-api-key {SECRET}"), "rejected the credentials"),
    (type("APIConnectionError", (Exception,), {})("connection refused"), "could not be reached"),
    (ValueError("/Users/someone/project/secret-path.py line 40"), "failed and was stopped"),
])
def test_a_failure_is_described_without_its_internals(exc, expected):
    message = observability.public_error(exc, "abcd1234")

    assert expected in message and "abcd1234" in message
    for leaked in (SECRET, "org-abc123", "/Users/", "secret-path", "/v1/messages"):
        assert leaked not in message


def test_a_message_written_for_the_user_is_passed_through():
    from pipeline.runtime_mode import NO_PROVIDER, ProviderUnavailable

    assert observability.public_error(ProviderUnavailable(NO_PROVIDER), "x") == NO_PROVIDER


def test_a_failed_job_tells_the_user_the_kind_and_logs_the_detail(client, monkeypatch, caplog):
    async def boom(request, *, progress_callback=None):
        raise ValueError(f"could not parse response containing {SECRET}")

    monkeypatch.setattr(server, "generate_article", boom)
    with caplog.at_level(logging.ERROR):
        job_id = client.post("/generate", json={
            "topic": "Kafka", "must_cover": ["rebalancing"]}).json()["job_id"]
        stream = client.get(f"/jobs/{job_id}/stream").text

    assert SECRET not in stream and "could not parse" not in stream
    assert job_id[:8] in stream and "ValueError" in stream
    assert any(job_id[:8] in r.getMessage() for r in caplog.records), \
        "the reference the user was given finds the failure in the log"


# ── Diagnostics ──────────────────────────────────────────────────────

def test_a_job_that_has_gone_quiet_is_reported_as_stalled(client, monkeypatch):
    monkeypatch.setattr(observability, "STALL_SECONDS", 0.05)

    async def scenario():
        job = jobs.create_job()
        await job.publish(ProgressEvent(type="stage_started", stage="drafting"))
        await asyncio.sleep(0.15)
        return job.job_id

    job_id = asyncio.run(scenario())
    found = client.get("/diagnostics").json()["articles"]

    assert found["running"] == 1
    assert [j["job"] for j in found["stalled"]] == [job_id[:8]]
    assert found["stalled"][0]["quiet_for"] >= 0.1


def test_stage_timings_come_from_the_events_already_recorded(client):
    async def scenario():
        job = jobs.create_job()
        await job.publish(ProgressEvent(type="stage_started", stage="brief"))
        await asyncio.sleep(0.05)
        await job.publish(ProgressEvent(type="stage_completed", stage="brief",
                                        data={"cached": True}))
        await job.publish(ProgressEvent(type="stage_started", stage="search"))
        await job.finish(jobs.COMPLETE, ProgressEvent(type="complete", stage="complete"))

    asyncio.run(scenario())
    finished = client.get("/diagnostics").json()["articles"]["recently_finished"][0]

    brief = next(s for s in finished["stages"] if s["stage"] == "brief")
    assert brief["seconds"] >= 0.04 and brief["cached"] is True
    assert any(s["stage"] == "search" and s["seconds"] is None for s in finished["stages"])


def test_usage_is_reported_as_not_measured_rather_than_guessed(client):
    usage = client.get("/diagnostics").json()["usage"]

    assert usage["measured"] is False and "not recorded" in usage["note"]


def test_diagnostics_holds_no_content_and_no_credentials(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    async def scenario():
        job = jobs.create_job()
        await job.publish(ProgressEvent(
            type="stage_started", stage="drafting",
            message="writing about the candidate's Halberd migration"))

    asyncio.run(scenario())
    text = client.get("/diagnostics").text

    assert SECRET not in text and "Halberd" not in text


# ── Shutdown ─────────────────────────────────────────────────────────

def test_a_clean_shutdown_leaves_running_work_marked_interrupted(client, tmp_path):
    from pipeline.schemas.models import ResumeDoc

    async def scenario():
        job = jobs.create_job()
        await job.publish(ProgressEvent(type="stage_started", stage="drafting"))
        server._RESUME_WORK.add("20260101-000000-abc123")
        server._save_resume_doc(ResumeDoc(
            resume_id="20260101-000000-abc123", original_text="x", status="analyzing"))
        await server._leave_things_recoverable()
        return job

    job = asyncio.run(scenario())

    assert job.state == jobs.INTERRUPTED
    record = (tmp_path / "_jobs" / f"{job.job_id}.json").read_text()
    assert '"interrupted"' in record
    assert server._load_resume_doc("20260101-000000-abc123").status == "error"
    assert "cut short" in server._load_resume_doc("20260101-000000-abc123").error
