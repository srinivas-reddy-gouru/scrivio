"""What a killed server leaves behind, and what the next one makes of it
(review item R13).

These start a real server, put real work in flight, and SIGKILL the
process: no shutdown hook runs and nothing is tidied. A second, ordinary
server is then started over the same directory. Writing the files by
hand would test what I imagine a crash leaves; this tests what one does.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_server import LiveServer  # noqa: E402

SLOW = str(Path(__file__).with_name("slow_server.py"))
RESUME = "Sam Okafor\nSoftware Engineer at Initrode, Feb 2020 to Present\n" * 12


def events(server: LiveServer, job_id: str, last_event_id: str = "") -> list[dict]:
    """Read a job's stream to its end. Only for a job that HAS ended."""
    headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
    status, text = server.request("GET", f"/jobs/{job_id}/stream", headers=headers)
    assert status == 200
    found = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines()
                      if ": " in line and not line.startswith(":"))
        if "data" in fields and "id" in fields:
            found.append({"id": int(fields["id"]), **json.loads(fields["data"])})
    return found


def wait_for(condition, seconds=15.0, what="the condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def crashed(tmp_path):
    """A server with an article job, a resume analysis, and a tailoring
    run all in flight, killed without warning."""
    first = LiveServer(tmp_path, launcher=SLOW).start()
    try:
        status, job = first.json("POST", "/generate", {
            "topic": "Kafka rebalancing", "must_cover": ["pauses"]})
        assert status == 200, job
        job_id = job["job_id"]
        wait_for(lambda: (first.data / "_jobs" / f"{job_id}.events.jsonl").is_file()
                 and len((first.data / "_jobs" / f"{job_id}.events.jsonl")
                         .read_text().splitlines()) >= 4,
                 what="the job to report progress")

        _, analysing = first.json("POST", "/resumes", {"resume_text": RESUME})

        # A resume that finished analysis earlier, now being tailored.
        ready = first.data / "resumes" / "20260101-000000-abc123.json"
        ready.write_text(json.dumps({
            "resume_id": "20260101-000000-abc123", "original_text": RESUME,
            "status": "ready", "jd_text": "Senior engineer. Python.",
            "structured": {"basics": {"name": "Sam Okafor"}},
        }))
        status, _ = first.json("POST", "/resumes/20260101-000000-abc123/tailor")
        assert status == 200

        assert first.json("GET", f"/jobs/{job_id}")[1]["status"] == "pending"
    finally:
        first.kill()

    second = LiveServer(tmp_path)
    second.port, second.base = first.port, first.base
    second.start()
    yield second, job_id, analysing["resume_id"], "20260101-000000-abc123"
    second.stop()


def test_an_article_job_cut_short_is_reported_as_interrupted(crashed):
    server, job_id, _, _ = crashed

    status, body = server.json("GET", f"/jobs/{job_id}")

    assert status == 200, "the job must still be known after a restart"
    assert body["status"] == "interrupted"
    assert "cut short" in body["error"] and "Start it again" in body["error"]


def test_its_stream_replays_what_happened_and_then_ends(crashed):
    server, job_id, _, _ = crashed

    seen = events(server, job_id)

    assert [e["stage"] for e in seen] == ["brief", "brief", "search", "search", "interrupted"]
    assert [e["id"] for e in seen] == [1, 2, 3, 4, 5], "in order, numbered, none missing"
    assert seen[-1]["type"] == "error"


def test_nothing_is_started_again_by_itself(crashed):
    """Every one of these is a run of paid model calls."""
    server, job_id, analysing, tailoring = crashed
    time.sleep(1.5)

    assert server.json("GET", f"/jobs/{job_id}")[1]["status"] == "interrupted"
    assert server.json("GET", f"/resumes/{analysing}")[1]["status"] == "error"
    records = list((server.data / "_jobs").glob("*.json"))
    assert len(records) == 1, "no new job appeared"


def test_a_resume_analysis_cut_short_says_so_and_keeps_the_upload(crashed):
    server, _, analysing, _ = crashed

    _, doc = server.json("GET", f"/resumes/{analysing}")

    assert doc["status"] == "error"
    assert "cut short" in doc["error"] and "Analyze again" in doc["error"]
    assert doc["original_text"].startswith("Sam Okafor"), "the upload is intact"
    assert doc["report"]["score"] > 0, "and so is the checklist that had shipped"


def test_a_tailoring_run_cut_short_says_so(crashed):
    server, _, _, tailoring = crashed

    _, doc = server.json("GET", f"/resumes/{tailoring}")

    assert doc["tailor_status"] == "error"
    assert "cut short" in doc["tailor_error"]
    assert doc["status"] == "ready", "the analysis that had finished is untouched"


def test_the_interrupted_work_can_be_asked_for_again(crashed):
    """The retry exists and is the user's to press. Whether it then
    succeeds depends on a provider, which this server does not have."""
    server, _, analysing, _ = crashed

    status, doc = server.json("POST", f"/resumes/{analysing}/analyze")

    assert status == 200 and doc["status"] == "analyzing"
    final = wait_for(
        lambda: (d := server.json("GET", f"/resumes/{analysing}")[1])["status"] != "analyzing" and d,
        what="the retried analysis to settle")
    assert final["status"] == "error" and "provider" in final["error"].lower()


def test_health_and_listings_work_straight_after_a_crash(crashed):
    server, _, analysing, tailoring = crashed

    assert server.json("GET", "/health") == (200, {"ok": True})
    status, listing = server.json("GET", "/resumes")
    assert status == 200
    assert {r["resume_id"] for r in listing} == {analysing, tailoring}


def test_a_server_that_dies_on_startup_says_why(tmp_path):
    """The helper used to discard what the server wrote, so a startup
    failure in the suite read only as "exited during startup"."""
    launcher = tmp_path / "broken.py"
    launcher.write_text("raise SystemExit('the reason it could not start')\n")
    with pytest.raises(RuntimeError, match="the reason it could not start"):
        LiveServer(tmp_path / "root", launcher=str(launcher)).start()
