import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import jobs, server
from main import MockAnthropicClient
from pipeline.schemas.models import (
    ArticleRequest,
    ProgressEvent,
    PublishedArticle,
)


@pytest.fixture(autouse=True)
def _reset_jobs():
    jobs.clear_jobs()
    yield
    jobs.clear_jobs()


@pytest.fixture(autouse=True)
def _isolate_output_dir(monkeypatch):
    """Redirect article persistence to a tmp dir so tests don't pollute ./output."""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(server, "OUTPUT_ROOT", Path(td))
        yield


@pytest.fixture(autouse=True)
def _mock_anthropic_for_api(monkeypatch):
    """Force the API server to use MockAnthropicClient regardless of env keys.

    Without this, the classifier and questions worker would call real LLMs
    when ANTHROPIC_API_KEY is set (e.g. from a project .env). Tests must be
    hermetic — every LLM call goes through the deterministic mock instead.
    """
    monkeypatch.setattr(
        server, "_anthropic_client", lambda request: MockAnthropicClient(request)
    )


def test_health_endpoint() -> None:
    client = TestClient(server.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_generate_returns_job_id_and_streams_events(monkeypatch) -> None:
    """End-to-end: POST /generate with skip_clarification, stream events,
    get a 'complete' event last. skip_clarification=true bypasses the new
    classifier so this existing path keeps working unchanged."""
    sample_article = PublishedArticle(
        request=ArticleRequest(topic="testing"),
        title="Test article",
        markdown="# Test\n\nBody.",
    )

    async def fake_generate(request, *, progress_callback=None):
        # Emit two progress events then return.
        if progress_callback:
            await progress_callback(
                ProgressEvent(type="stage_started", stage="brief", message="working")
            )
            await progress_callback(
                ProgressEvent(type="stage_completed", stage="brief")
            )
        return {"intermediate": sample_article}

    monkeypatch.setattr(server, "generate_article", fake_generate)

    client = TestClient(server.app)
    response = client.post(
        "/generate",
        json={"topic": "testing", "skip_clarification": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("clarification_required") is False
    job_id = body["job_id"]

    # The background task may not have started yet; the stream blocks until events arrive.
    with client.stream("GET", f"/jobs/{job_id}/stream") as stream_response:
        assert stream_response.status_code == 200
        events = []
        for line in stream_response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            events.append(json.loads(line[len("data: ") :]))

    types = [e["type"] for e in events]
    assert "stage_started" in types
    assert "stage_completed" in types
    assert types[-1] == "complete"
    complete_event = events[-1]
    assert "output_dir" in complete_event["data"]
    assert complete_event["data"]["articles"]["intermediate"]["title"] == "Test article"


def test_stream_returns_404_for_unknown_job() -> None:
    client = TestClient(server.app)
    response = client.get("/jobs/does-not-exist/stream")
    assert response.status_code == 404


def test_job_status_returns_error_when_generate_raises(monkeypatch) -> None:
    async def boom(request, *, progress_callback=None):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "generate_article", boom)

    client = TestClient(server.app)
    job_id = client.post(
        "/generate",
        json={"topic": "testing", "skip_clarification": True},
    ).json()["job_id"]

    # Drain the stream so the background task runs to completion.
    with client.stream("GET", f"/jobs/{job_id}/stream") as stream_response:
        for _ in stream_response.iter_lines():
            pass

    status = client.get(f"/jobs/{job_id}").json()
    assert status["status"] == "error"
    assert "kaboom" in status["error"]


# ── Sprint 2: clarification-first flow ─────────────────────────────────

def test_generate_returns_clarification_for_broad_unsteered_topic() -> None:
    """When a broad topic arrives with no steering, /generate must NOT start
    a job — it must return clarification_required with questions for the user."""
    client = TestClient(server.app)
    response = client.post("/generate", json={"topic": "database"})

    assert response.status_code == 200
    body = response.json()
    assert body["clarification_required"] is True
    assert body.get("job_id") is None
    assert len(body["questions"]) >= 2  # At minimum: scope + must_cover.
    assert body["default_if_skipped"]  # Non-empty summary.
    # Every question must have an id and question field.
    for q in body["questions"]:
        assert q["id"]
        assert q["question"]


def test_generate_starts_job_when_skip_clarification_true(monkeypatch) -> None:
    """skip_clarification=true is the explicit opt-out — the system should
    proceed to generate even for a broad topic, using the default angle."""
    async def fake_generate(request, *, progress_callback=None):
        return {}

    monkeypatch.setattr(server, "generate_article", fake_generate)

    client = TestClient(server.app)
    response = client.post(
        "/generate",
        json={"topic": "database", "skip_clarification": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["clarification_required"] is False
    assert body["job_id"]  # Non-empty job_id.


def test_generate_starts_job_when_extra_context_provided(monkeypatch) -> None:
    """extra_context counts as steering — even a broad topic with non-empty
    extra_context skips clarification."""
    async def fake_generate(request, *, progress_callback=None):
        return {}

    monkeypatch.setattr(server, "generate_article", fake_generate)

    client = TestClient(server.app)
    response = client.post(
        "/generate",
        json={
            "topic": "database",
            "extra_context": "Focus on ACID guarantees and isolation levels.",
        },
    )

    assert response.status_code == 200
    assert response.json()["job_id"]


def test_generate_starts_job_when_must_cover_provided(monkeypatch) -> None:
    """must_cover counts as steering."""
    async def fake_generate(request, *, progress_callback=None):
        return {}

    monkeypatch.setattr(server, "generate_article", fake_generate)

    client = TestClient(server.app)
    response = client.post(
        "/generate",
        json={"topic": "database", "must_cover": ["MVCC", "indexes"]},
    )

    assert response.status_code == 200
    assert response.json()["job_id"]


def test_generate_starts_job_and_composes_clarification_answers_into_context(
    monkeypatch,
) -> None:
    """When clarification_answers are present, /generate must:
       (a) treat the request as steered (no further clarification),
       (b) compose answers into extra_context before the brief sees the request."""
    captured_requests: list[ArticleRequest] = []

    async def fake_generate(request, *, progress_callback=None):
        captured_requests.append(request)
        return {}

    monkeypatch.setattr(server, "generate_article", fake_generate)

    client = TestClient(server.app)
    response = client.post(
        "/generate",
        json={
            "topic": "database",
            "clarification_answers": {
                "scope": "relational (SQL)",
                "angle": "fundamentals/explainer",
                "must_cover": "indexing strategies, query planning",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["job_id"]
    # Wait briefly for the background task to run.
    import time
    time.sleep(0.1)

    assert len(captured_requests) == 1
    composed = captured_requests[0].extra_context
    # All three answers must appear in the composed extra_context.
    assert "scope: relational (SQL)" in composed
    assert "angle: fundamentals/explainer" in composed
    # must_cover free-text was split into the must_cover list AND echoed in
    # extra_context so the brief, planner, and editor all see it.
    must_cover = captured_requests[0].must_cover
    assert "indexing strategies" in must_cover
    assert "query planning" in must_cover


def test_generate_starts_job_for_narrow_topic(monkeypatch) -> None:
    """Narrow topics (specific verbs, symptoms, errors) must skip
    clarification entirely. The heuristic recognizes these without any LLM call."""
    async def fake_generate(request, *, progress_callback=None):
        return {}

    monkeypatch.setattr(server, "generate_article", fake_generate)

    client = TestClient(server.app)
    response = client.post(
        "/generate",
        json={"topic": "Why does Postgres VACUUM block writes under high load?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["clarification_required"] is False
    assert body["job_id"]


def test_clarify_endpoint_returns_questions_for_broad_topic() -> None:
    client = TestClient(server.app)
    response = client.post("/clarify", json={"topic": "database"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["questions"]) >= 2
    assert body["default_if_skipped"]


def test_clarify_endpoint_returns_empty_questions_for_narrow_topic() -> None:
    """Narrow topics need no clarification — /clarify returns an empty list
    rather than erroring, so the frontend has a single uniform code path."""
    client = TestClient(server.app)
    response = client.post(
        "/clarify",
        json={"topic": "Why does Postgres VACUUM block writes under high load?"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["questions"] == []


# ── Cancel endpoint (UI Stop button) ──────────────────────────────────

def test_delete_job_returns_404_for_unknown_job() -> None:
    client = TestClient(server.app)
    response = client.delete("/jobs/does-not-exist")
    assert response.status_code == 404


def test_job_cancel_method_signals_in_flight_task() -> None:
    """Unit test on Job.cancel() directly — verifies the real cancellation
    mechanic without TestClient's per-request event-loop teardown
    interfering. When a task is parked on `await`, cancel() returns True
    and a `CancelledError` propagates at the await point."""
    from api.jobs import Job

    async def run() -> tuple[bool, bool]:
        job = Job("test-job")

        async def worker() -> None:
            await asyncio.sleep(60)

        job.task = asyncio.create_task(worker())
        # Let the task actually start.
        await asyncio.sleep(0)
        cancelled = job.cancel()
        # Wait for the cancellation to take effect.
        try:
            await job.task
        except asyncio.CancelledError:
            pass
        return cancelled, job.cancelled

    cancelled, sticky_flag = asyncio.run(run())
    assert cancelled is True
    assert sticky_flag is True


def test_job_cancel_returns_false_when_task_already_done() -> None:
    from api.jobs import Job

    async def run() -> bool:
        job = Job("test-job")

        async def quick() -> None:
            return

        job.task = asyncio.create_task(quick())
        await job.task
        return job.cancel()

    assert asyncio.run(run()) is False


def test_delete_job_endpoint_returns_200_and_proper_shape(monkeypatch) -> None:
    """End-to-end HTTP test on the cancel endpoint. With TestClient the
    spawned pipeline task may have been torn down by the time DELETE
    arrives (per-request loop teardown), so we don't assert on the
    `cancelled` bool — just that the endpoint returns 200 with the
    expected schema and that the status endpoint reports `cancelled`
    afterwards. The actual cancellation mechanic is covered by the unit
    tests above."""
    async def slow_generate(request, *, progress_callback=None):
        if progress_callback:
            await progress_callback(
                ProgressEvent(type="stage_started", stage="brief")
            )
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(server, "generate_article", slow_generate)

    client = TestClient(server.app)
    job_id = client.post(
        "/generate",
        json={"topic": "testing", "skip_clarification": True},
    ).json()["job_id"]

    response = client.delete(f"/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert "cancelled" in body
    assert body["job_id"] == job_id

    # After DELETE, the status endpoint must report cancelled regardless
    # of whether the task was actually mid-flight at the moment we hit
    # DELETE — Job.cancel() sets the sticky flag synchronously.
    status = client.get(f"/jobs/{job_id}").json()
    assert status["status"] == "cancelled"


# ── Article history endpoints ────────────────────────────────────────

def _write_article(output_root: Path, dir_name: str, *, topic: str, level: str = "intermediate",
                   title: str = "Test article", markdown: str = "# Test\n\nBody.",
                   generated_at: str = "2026-05-21T00:00:00") -> Path:
    """Test helper: simulate what api.server._persist_job writes to disk."""
    article_dir = output_root / dir_name
    article_dir.mkdir(parents=True, exist_ok=True)
    (article_dir / f"{level}.md").write_text(markdown, encoding="utf-8")
    meta = {
        "job_id": "test-job",
        "generated_at": generated_at,
        "title": title,
        "request": {
            "topic": topic,
            "explanation_level": level,
        },
        "verification_reports": [],
        "assets": [],
    }
    (article_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return article_dir


def test_list_articles_returns_empty_when_output_dir_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path / "does-not-exist")
    client = TestClient(server.app)
    response = client.get("/articles")
    assert response.status_code == 200
    assert response.json() == []


def test_list_articles_returns_metadata_for_each_article(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    _write_article(tmp_path, "20260521-000000__kafka__abc", topic="Kafka", title="Kafka explained")
    _write_article(tmp_path, "20260521-010000__spring__def", topic="Spring", title="Spring intro")

    response = TestClient(server.app).get("/articles")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    titles = {a["title"] for a in body}
    assert titles == {"Kafka explained", "Spring intro"}
    # Each summary carries the fields the UI needs to render a card.
    for a in body:
        assert "id" in a and "topic" in a and "level" in a
        assert "available_levels" in a
        assert "intermediate" in a["available_levels"]


def test_list_articles_returns_newest_first(monkeypatch, tmp_path) -> None:
    """History should sort by mtime descending so the freshest article
    appears at the top. The user sees their most recent work first."""
    import time
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    _write_article(tmp_path, "older__abc", topic="Old topic", title="Older article")
    # Force a measurable mtime difference even on fast filesystems.
    time.sleep(0.05)
    _write_article(tmp_path, "newer__def", topic="New topic", title="Newer article")

    body = TestClient(server.app).get("/articles").json()
    assert body[0]["title"] == "Newer article"
    assert body[1]["title"] == "Older article"


def test_list_articles_skips_directories_without_meta(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    # A directory with markdown but no meta.json — old format or partial write.
    (tmp_path / "partial__xyz").mkdir()
    (tmp_path / "partial__xyz" / "intermediate.md").write_text("body")
    _write_article(tmp_path, "good__abc", topic="Good", title="Good article")

    body = TestClient(server.app).get("/articles").json()
    assert len(body) == 1
    assert body[0]["title"] == "Good article"


def test_list_articles_respects_limit_parameter(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    for i in range(5):
        _write_article(tmp_path, f"a{i}__xyz", topic=f"T{i}", title=f"Article {i}")

    body = TestClient(server.app).get("/articles?limit=3").json()
    assert len(body) == 3


def test_get_article_returns_markdown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    _write_article(tmp_path, "kafka__abc", topic="Kafka",
                   title="Kafka explained", markdown="# Kafka\n\nDistributed log.")

    response = TestClient(server.app).get("/articles/kafka__abc")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "kafka__abc"
    assert body["title"] == "Kafka explained"
    assert body["markdown"] == "# Kafka\n\nDistributed log."
    assert body["available_levels"] == ["intermediate"]


def test_get_article_rejects_invalid_id_characters(monkeypatch, tmp_path) -> None:
    """The id regex protects against any character that isn't a safe
    identifier: spaces, dots, slashes (those that survive URL normalization),
    percent-encoded payloads. Bare `..` and `.` are URL-normalized by the
    HTTP client before they reach the handler, so we don't test those."""
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    # Create a real article in the tmp output dir so a real-looking id
    # actually exists (rules out the "404 because nothing exists" case).
    _write_article(tmp_path, "real-article__xyz", topic="ok")

    for bad_id in ["foo bar", "foo.bar", "..%2Fetc%2Fpasswd", "with spaces"]:
        response = TestClient(server.app).get(f"/articles/{bad_id}")
        assert response.status_code == 404, f"invalid id {bad_id!r} should 404"


def test_get_article_returns_404_for_unknown_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    response = TestClient(server.app).get("/articles/does-not-exist")
    assert response.status_code == 404


def test_get_article_supports_level_query_parameter(monkeypatch, tmp_path) -> None:
    """When an article has multiple levels on disk, the UI can request
    a specific one via ?level=basic."""
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    article_dir = _write_article(tmp_path, "multi__xyz", topic="Kafka",
                                  title="Kafka explained", level="intermediate")
    # Add a basic.md too.
    (article_dir / "basic.md").write_text("# Kafka basic version", encoding="utf-8")

    response = TestClient(server.app).get("/articles/multi__xyz?level=basic")
    assert response.status_code == 200
    body = response.json()
    assert body["level"] == "basic"
    assert "basic version" in body["markdown"]


def test_get_article_returns_404_for_unavailable_level(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    _write_article(tmp_path, "kafka__abc", topic="Kafka", level="intermediate")

    response = TestClient(server.app).get("/articles/kafka__abc?level=advanced")
    assert response.status_code == 404


def test_delete_job_returns_false_for_already_finished_job(monkeypatch) -> None:
    """Cancelling a job that already completed isn't an error — it returns
    200 with `cancelled: false` so the UI can simply ignore the response."""
    async def quick_generate(request, *, progress_callback=None):
        if progress_callback:
            await progress_callback(
                ProgressEvent(type="stage_started", stage="brief")
            )
            await progress_callback(
                ProgressEvent(type="stage_completed", stage="brief")
            )
        return {}

    monkeypatch.setattr(server, "generate_article", quick_generate)

    client = TestClient(server.app)
    job_id = client.post(
        "/generate",
        json={"topic": "testing", "skip_clarification": True},
    ).json()["job_id"]

    # Drain the stream so the task completes.
    with client.stream("GET", f"/jobs/{job_id}/stream") as stream_response:
        for _ in stream_response.iter_lines():
            pass

    response = client.delete(f"/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["cancelled"] is False
