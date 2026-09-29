"""Job state, lost updates, and event delivery (review items R13, R14).

The restart tests that kill a real server are in tests/live. These run
in process, where timing can be controlled exactly: a pipeline that waits
at a gate until the test opens it.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from api import jobs, server
from api.jobs import Gap, Heartbeat, Job
from pipeline.schemas.models import (
    ProgressEvent, ResumeDoc, StructuredResume, TailoredResume,
)


@pytest.fixture(autouse=True)
def fresh_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    jobs.configure(lambda: server.OUTPUT_ROOT / "_jobs")
    jobs.clear_jobs()
    yield
    jobs.clear_jobs()


def progress(stage: str, kind: str = "stage_started") -> ProgressEvent:
    return ProgressEvent(type=kind, stage=stage)


def done() -> ProgressEvent:
    return ProgressEvent(type="complete", stage="complete", data={"output_dir": "/x/y"})


async def collect(job: Job, after: int = 0, heartbeat: float = 0.05, limit: int = 1000):
    seen = []
    async for item in job.subscribe(after, heartbeat=heartbeat):
        if isinstance(item, Heartbeat):
            continue
        seen.append(item)
        if len(seen) >= limit:
            break
    return seen


# ── R14: delivery ────────────────────────────────────────────────────

def test_two_subscribers_each_see_the_whole_sequence_in_order():
    """They used to share one queue and split the events between them."""
    async def scenario():
        job = jobs.create_job()
        first = asyncio.create_task(collect(job))
        second = asyncio.create_task(collect(job))
        await asyncio.sleep(0.01)
        for stage in ("brief", "search", "planning", "drafting"):
            await job.publish(progress(stage))
        await job.finish(jobs.COMPLETE, done())
        return await first, await second

    first, second = asyncio.run(scenario())

    expected = ["brief", "search", "planning", "drafting", "complete"]
    assert [e.stage for _, e in first] == expected
    assert [e.stage for _, e in second] == expected
    assert [seq for seq, _ in first] == [1, 2, 3, 4, 5]


def test_a_reconnect_resumes_after_the_last_event_it_saw():
    async def scenario():
        job = jobs.create_job()
        for stage in ("brief", "search", "planning"):
            await job.publish(progress(stage))
        before = await collect(job, limit=2)             # then the connection drops
        await job.publish(progress("drafting"))
        await job.finish(jobs.COMPLETE, done())
        after = await collect(job, after=before[-1][0])
        return before, after

    before, after = asyncio.run(scenario())

    assert [e.stage for _, e in before] == ["brief", "search"]
    assert [e.stage for _, e in after] == ["planning", "drafting", "complete"], \
        "nothing missed, nothing repeated"


def test_connecting_after_the_end_gets_the_record_and_closes():
    """This used to wait for ever: the sentinel that ended the stream had
    already been taken by whoever was listening at the time."""
    async def scenario():
        job = jobs.create_job()
        await job.publish(progress("brief"))
        await job.finish(jobs.COMPLETE, done())
        return await asyncio.wait_for(collect(job), timeout=2)

    seen = asyncio.run(scenario())

    assert [e.type for _, e in seen] == ["stage_started", "complete"]


def test_a_quiet_stream_sends_heartbeats():
    async def scenario():
        job = jobs.create_job()
        stream = job.subscribe(heartbeat=0.05)
        first = await asyncio.wait_for(stream.__anext__(), timeout=2)
        await stream.aclose()
        return first

    assert isinstance(asyncio.run(scenario()), Heartbeat)


def test_a_subscriber_that_leaves_holds_nothing_on_the_server():
    async def scenario():
        job = jobs.create_job()
        before = set(vars(job))
        for _ in range(50):
            stream = job.subscribe(heartbeat=0.01)
            await stream.__anext__()
            await stream.aclose()                        # the client went away
        return before, set(vars(job)), len(job._changed._waiters)

    before, after, waiting = asyncio.run(scenario())

    assert before == after, "no per-subscriber state is kept on the job"
    assert waiting == 0


def test_memory_is_bounded_and_older_events_come_from_the_log(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_EVENTS_IN_MEMORY", 10)

    async def scenario():
        job = jobs.create_job()
        for i in range(40):
            await job.publish(progress(f"stage-{i}"))
        await job.finish(jobs.COMPLETE, done())
        return len(job._events), await collect(job)

    held, seen = asyncio.run(scenario())

    assert held == 10
    assert [seq for seq, _ in seen] == list(range(1, 42)), "the full record, from disk"


def test_a_job_cannot_log_without_end(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_EVENTS", 25)

    async def scenario():
        job = jobs.create_job()
        for i in range(200):
            await job.publish(progress(f"stage-{i}"))
        return job

    job = asyncio.run(scenario())

    assert job.last_seq == 25
    assert len(job._log_path.read_text().splitlines()) == 25


def test_a_gap_in_the_record_is_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_EVENTS_IN_MEMORY", 5)

    async def scenario():
        job = jobs.create_job()
        for i in range(20):
            await job.publish(progress(f"stage-{i}"))
        job._log_path.unlink()                           # the log is lost
        await job.finish(jobs.COMPLETE, done())
        return await collect(job, after=3)

    seen = asyncio.run(scenario())

    assert isinstance(seen[0], Gap)
    assert seen[0].after == 3 and seen[0].resumes_at > 4


def test_the_stream_endpoint_numbers_events_and_honours_last_event_id():
    async def scenario():
        job = jobs.create_job()
        for stage in ("brief", "search"):
            await job.publish(progress(stage))
        await job.finish(jobs.COMPLETE, done())
        return job.job_id

    job_id = asyncio.run(scenario())
    client = TestClient(server.app)

    whole = client.get(f"/jobs/{job_id}/stream").text
    resumed = client.get(f"/jobs/{job_id}/stream", headers={"Last-Event-ID": "2"}).text
    by_query = client.get(f"/jobs/{job_id}/stream?after=2").text

    assert [l for l in whole.splitlines() if l.startswith("id: ")] == ["id: 1", "id: 2", "id: 3"]
    assert [l for l in resumed.splitlines() if l.startswith("id: ")] == ["id: 3"]
    assert resumed.count("data: ") == 1 and '"complete"' in resumed
    assert by_query == resumed


# ── R13: state ───────────────────────────────────────────────────────

def test_a_cancel_stays_a_cancel_even_if_the_pipeline_then_finishes():
    async def scenario():
        job = jobs.create_job()
        await job.publish(progress("brief"))
        assert job.cancel() is False            # no live task, but it is cancelled
        became_complete = await job.finish(jobs.COMPLETE, done())
        await job.publish(progress("late stage"))
        return job, became_complete, await collect(job)

    job, became_complete, seen = asyncio.run(scenario())

    assert became_complete is False and job.state == jobs.CANCELLED
    assert [e.type for _, e in seen] == ["stage_started", "cancelled"]


def test_cancelling_a_running_job_through_the_api_is_terminal(monkeypatch):
    gate = asyncio.Event()

    async def held(request, *, progress_callback=None):
        await progress_callback(progress("brief"))
        await gate.wait()
        return {}

    monkeypatch.setattr(server, "generate_article", held)
    client = TestClient(server.app)
    job_id = client.post("/generate", json={
        "topic": "Kafka", "must_cover": ["rebalancing"]}).json()["job_id"]

    assert client.delete(f"/jobs/{job_id}").status_code == 200

    assert client.get(f"/jobs/{job_id}").json()["status"] == "cancelled"
    stream = client.get(f"/jobs/{job_id}/stream").text
    assert '"cancelled"' in stream and '"complete"' not in stream


def test_the_same_request_twice_is_one_run(monkeypatch):
    runs = []
    gate = asyncio.Event()

    async def held(request, *, progress_callback=None):
        runs.append(request.topic)
        await gate.wait()
        return {}

    monkeypatch.setattr(server, "generate_article", held)
    monkeypatch.setattr(server, "_is_steered", lambda request: True)

    async def scenario():
        import httpx
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            body = {"topic": "Kafka", "must_cover": ["rebalancing"]}
            first = (await c.post("/generate", json=body)).json()["job_id"]
            second = (await c.post("/generate", json=body)).json()["job_id"]
            other = (await c.post("/generate", json={**body, "topic": "Spring"})).json()["job_id"]
            await asyncio.sleep(0.05)
            for job_id in (first, other):
                await c.delete(f"/jobs/{job_id}")
            return first, second, other

    first, second, other = asyncio.run(scenario())

    assert first == second, "a double click joins the run in progress"
    assert other != first, "a different request is a different run"
    assert sorted(runs) == ["Kafka", "Spring"]


def test_an_unreadable_job_record_is_set_aside_not_fatal(tmp_path):
    folder = tmp_path / "_jobs"
    folder.mkdir()
    job_id = "00000000-0000-0000-0000-00000000dead"
    (folder / f"{job_id}.json").write_text('{"job_id": "x", "state": ')

    client = TestClient(server.app)

    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert (folder / f"{job_id}.json.corrupt").is_file(), "kept, under a name that says why"
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("job_id", ["../../etc/passwd", "..%2F..%2Fsecret", "a/b", "x" * 300])
def test_a_job_id_is_never_a_path(job_id):
    assert jobs.get_job(job_id) is None


def test_old_job_records_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "KEEP_ON_DISK", 5)

    async def scenario():
        for _ in range(12):
            job = jobs.create_job()
            await job.finish(jobs.COMPLETE, done())

    asyncio.run(scenario())
    jobs.clear_jobs()

    jobs.recover()

    assert len(list((tmp_path / "_jobs").glob("*.json"))) == 5


# ── R13: lost updates on a resume ────────────────────────────────────

RESUME = {
    "basics": {"name": "Sam Okafor", "summary": "Engineer."},
    "work": [{"name": "Initrode", "position": "Engineer", "startDate": "2020",
              "endDate": "Present", "highlights": ["Maintained 12 services"]}],
}


@pytest.fixture
def resume():
    structured = StructuredResume.model_validate(RESUME)
    doc = ResumeDoc(
        resume_id="20260101-000000-abc123", original_text="Sam Okafor",
        structured=structured, jd_text="Senior engineer. Python.",
        tailored=TailoredResume(resume=structured.model_copy(deep=True),
                                changes=[], warnings=[]))
    server._save_resume_doc(doc)
    return doc.resume_id


def test_a_finished_tailoring_run_does_not_overwrite_a_newer_change(resume, monkeypatch):
    """The task loaded the document, waited on a model, and saved its own
    copy back over whatever had happened in between."""
    gate = asyncio.Event()

    async def slow_tailor(**kwargs):
        await gate.wait()
        return TailoredResume(
            resume=StructuredResume.model_validate(RESUME), changes=[],
            warnings=["from the tailoring run"])

    monkeypatch.setattr(server, "tailor_resume", slow_tailor)
    monkeypatch.setattr(server, "_client_for_session", lambda *a, **k: (object(), "balanced"))

    async def scenario():
        server._RESUME_WORK.add(resume)
        task = asyncio.create_task(server._finish_resume_tailor(resume))
        await asyncio.sleep(0.02)
        newer = server._load_resume_doc(resume)          # something else changes it
        newer.jd_label = "changed while tailoring ran"
        server._save_resume_doc(newer)
        gate.set()
        await task
        server._RESUME_WORK.discard(resume)

    asyncio.run(scenario())
    final = server._load_resume_doc(resume)

    assert final.jd_label == "changed while tailoring ran", "the newer change survived"
    assert final.tailored.warnings == ["from the tailoring run"], "and so did the result"
    assert final.tailor_status == "idle"


def test_a_finished_task_does_not_bring_back_a_deleted_resume(resume, monkeypatch):
    gate = asyncio.Event()

    async def slow_tailor(**kwargs):
        await gate.wait()
        return TailoredResume(resume=StructuredResume.model_validate(RESUME),
                              changes=[], warnings=[])

    monkeypatch.setattr(server, "tailor_resume", slow_tailor)
    monkeypatch.setattr(server, "_client_for_session", lambda *a, **k: (object(), "balanced"))

    async def scenario():
        server._RESUME_WORK.add(resume)
        task = asyncio.create_task(server._finish_resume_tailor(resume))
        await asyncio.sleep(0.02)
        server._resume_path(resume).unlink()             # the user deletes it
        gate.set()
        await task
        server._RESUME_WORK.discard(resume)

    asyncio.run(scenario())

    assert not server._resume_path(resume).exists()
    assert list(server._resumes_root().glob("*.tmp")) == []


def test_a_second_change_during_a_slow_edit_is_refused_not_lost(resume, monkeypatch):
    gate = asyncio.Event()

    async def slow_edit(**kwargs):
        await gate.wait()
        return kwargs["tailored"].model_copy(deep=True)

    monkeypatch.setattr(server, "edit_resume_by_instruction", slow_edit)
    monkeypatch.setattr(server, "_client_for_session", lambda *a, **k: (object(), "balanced"))

    async def scenario():
        import httpx
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            edit = asyncio.create_task(c.post(
                f"/resumes/{resume}/request-edit", json={"instruction": "tidy the wording"}))
            await asyncio.sleep(0.05)
            attempts = {
                "add": await c.post(f"/resumes/{resume}/add", json={
                    "kind": "bullet", "parent": "work[0]", "text": "Ran the on-call rota"}),
                "undo": await c.post(f"/resumes/{resume}/undo-tailored"),
                "second edit": await c.post(f"/resumes/{resume}/request-edit",
                                            json={"instruction": "shorter"}),
            }
            gate.set()
            return (await edit).status_code, {k: v.status_code for k, v in attempts.items()}

    first, others = asyncio.run(scenario())

    assert first == 200
    assert others == {"add": 409, "undo": 409, "second edit": 409}


def test_two_saves_of_one_resume_do_not_share_a_temp_file(resume):
    doc = server._load_resume_doc(resume)

    for _ in range(20):
        server._save_resume_doc(doc)

    assert [p.name for p in server._resumes_root().iterdir()] == [f"{resume}.json"]


def test_a_damaged_resume_file_is_set_aside_and_the_rest_still_list(resume):
    server._resume_path(resume).write_text('{"resume_id": "20260101-000000-abc123", "sta')
    good = ResumeDoc(resume_id="20260202-000000-fff000", original_text="x",
                     structured=StructuredResume.model_validate(RESUME))
    server._save_resume_doc(good)
    client = TestClient(server.app)

    opened = client.get(f"/resumes/{resume}")
    listing = client.get("/resumes")

    assert opened.status_code == 410
    assert "damaged" in opened.json()["detail"] and "Upload" in opened.json()["detail"]
    assert server._resume_path(resume).with_suffix(".json.corrupt").is_file()
    assert [r["resume_id"] for r in listing.json()] == ["20260202-000000-fff000"]


def test_documents_saved_before_these_changes_still_open():
    """A saved file from an earlier version, with none of the newer fields."""
    old = {"resume_id": "20250101-000000-0ld000", "original_text": "Sam Okafor",
           "structured": {"basics": {"name": "Sam Okafor"},
                          "work": [{"name": "Initrode", "position": "Engineer"}]}}
    path = server._resume_path("20250101-000000-0ld000")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old))

    doc = TestClient(server.app).get("/resumes/20250101-000000-0ld000")

    assert doc.status_code == 200
    assert doc.json()["status"] == "ready" and doc.json()["structured"]["custom"] == []
