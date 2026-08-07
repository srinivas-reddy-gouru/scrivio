import base64
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import server
from main import MockAnthropicClient


JD = (
    "Senior Backend Engineer at Acme. Requirements: 5+ years Java, Kafka "
    "event pipelines in production, Kubernetes deployment experience, "
    "cross-team collaboration. You will design event-driven systems."
)
RESUME = (
    "Srinivas Reddy — Backend Engineer. Built Kafka pipelines processing "
    "2M events/day at BigCo. Java, Spring Boot, PostgreSQL. Led migration "
    "to event-driven architecture."
)
STRONG_ANSWER = (
    "At BigCo I designed the ingestion pipeline with idempotent consumers "
    "and a dead letter queue so replays never double-charged accounts."
)
SHORT_ANSWER = "I used Kafka there."


@pytest.fixture(autouse=True)
def _isolate_output_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(server, "OUTPUT_ROOT", Path(td))
        server._interview_locks.clear()
        yield


@pytest.fixture(autouse=True)
def _mock_anthropic_for_api(monkeypatch):
    monkeypatch.setattr(
        server, "_anthropic_client", lambda request: MockAnthropicClient(request)
    )


@pytest.fixture(autouse=True)
def _no_web_search(monkeypatch):
    async def fake_research(profile):
        return [f"{profile.role_title} interview question — commonly asked"]

    monkeypatch.setattr(server, "research_job_questions", fake_research)

    async def fake_study_search(queries, **kwargs):
        from pipeline.workers.search_worker import SearchResult
        return [
            SearchResult(url="https://kubernetes.io/docs/concepts/",
                         title="Kubernetes Concepts", snippet="Official docs"),
            SearchResult(url="https://spam-farm.biz/guide/k8s",
                         title="Ultimate K8s Guide", snippet="SEO bait"),
        ]

    from pipeline.workers import job_interviewer_worker
    monkeypatch.setattr(job_interviewer_worker, "multi_search", fake_study_search)


def _create_profile(client: TestClient, **overrides) -> dict:
    body = {
        "role_title": "Senior Backend Engineer",
        "company": "Acme",
        "location": "Austin, TX",
        "seniority": "senior",
        "job_description": JD,
        "resume_text": RESUME,
    }
    body.update(overrides)
    response = client.post("/job-profiles", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ── Profiles ─────────────────────────────────────────────────────────

def test_create_profile_returns_analysis() -> None:
    client = TestClient(server.app)
    data = _create_profile(client)
    assert data["profile"]["role_title"] == "Senior Backend Engineer"
    names = [c["name"] for c in data["analysis"]["competencies"]]
    assert "Event streaming" in names            # from mock analysis
    assert data["analysis"]["gaps"]              # gap surfaced
    # Persisted on disk.
    path = server.OUTPUT_ROOT / "job_profiles" / f"{data['profile']['profile_id']}.json"
    assert path.is_file()


def test_create_profile_with_resume_file() -> None:
    client = TestClient(server.app)
    data = _create_profile(
        client,
        resume_text="",
        resume_file_b64=base64.b64encode(RESUME.encode()).decode(),
        resume_filename="resume.txt",
    )
    assert "Kafka pipelines" in data["profile"]["resume_text"]


def test_create_profile_bad_resume_file_actionable_422() -> None:
    client = TestClient(server.app)
    response = client.post("/job-profiles", json={
        "role_title": "Engineer", "job_description": JD,
        "resume_file_b64": base64.b64encode(b"x").decode(),
        "resume_filename": "resume.pages",
    })
    assert response.status_code == 422
    assert "Unsupported file type" in response.json()["detail"]


def test_create_profile_requires_jd_and_resume() -> None:
    client = TestClient(server.app)
    assert client.post("/job-profiles", json={
        "role_title": "Engineer", "resume_text": RESUME,
    }).status_code == 422
    assert client.post("/job-profiles", json={
        "role_title": "Engineer", "job_description": JD,
    }).status_code == 422


def test_profile_list_get_delete() -> None:
    client = TestClient(server.app)
    pid = _create_profile(client)["profile"]["profile_id"]
    assert [p["profile_id"] for p in client.get("/job-profiles").json()] == [pid]
    assert client.get(f"/job-profiles/{pid}").status_code == 200
    assert client.get("/job-profiles/..%2Fescape").status_code == 404
    assert client.delete(f"/job-profiles/{pid}").json()["deleted"] == pid
    assert client.get(f"/job-profiles/{pid}").status_code == 404


# ── Job sessions ─────────────────────────────────────────────────────

def _start_job_session(client: TestClient, duration: int = 30) -> dict:
    pid = _create_profile(client)["profile"]["profile_id"]
    response = client.post("/interviews", json={
        "mode": "job", "job_profile_id": pid, "duration_minutes": duration,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_job_session_created_with_segments() -> None:
    client = TestClient(server.app)
    session = _start_job_session(client)
    assert session["mode"] == "job"
    assert session["duration_minutes"] == 30
    assert session["topic"] == "Senior Backend Engineer @ Acme"
    anchors = [q["section_anchor"] for q in session["questions"]]
    assert "resume deep-dive" in anchors
    # Rubrics (with competency tags) hidden while open.
    assert all(q["rubric_key_points"] is None for q in session["questions"])


def test_job_mode_requires_profile_id() -> None:
    client = TestClient(server.app)
    assert client.post("/interviews", json={"mode": "job"}).status_code == 422


def test_job_session_redacts_feedback_but_allows_followup() -> None:
    client = TestClient(server.app)
    session = _start_job_session(client)
    sid = session["session_id"]
    qids = [q["id"] for q in session["questions"]]

    # Short answer → follow-up (allowed in job mode) but NO evaluation shown.
    first = client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qids[0], "answer": SHORT_ANSWER},
    ).json()
    assert first["evaluation"] is None            # redacted mid-session
    assert first["followup_question"]             # but the probe is visible
    assert first["question"]["first_evaluation"] is None

    # Answer the follow-up → completed, still redacted.
    second = client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qids[0], "answer": STRONG_ANSWER},
    ).json()
    assert second["question"]["status"] == "completed"
    assert second["evaluation"] is None


def test_job_completion_builds_scorecard_with_citations() -> None:
    client = TestClient(server.app)
    session = _start_job_session(client)
    sid = session["session_id"]
    qids = [q["id"] for q in session["questions"]]

    last = None
    for qid in qids:
        last = client.post(
            f"/interviews/{sid}/answers",
            json={"question_id": qid, "answer": STRONG_ANSWER},
        ).json()
    assert last["session_complete"] is True

    scorecard = last["summary"]["scorecard"]
    assert scorecard, "scorecard missing"
    names = [c["name"] for c in scorecard["competency_scores"]]
    assert "Event streaming" in names
    streaming = next(c for c in scorecard["competency_scores"] if c["name"] == "Event streaming")
    assert streaming["score"] == 8.0              # mock strong answers
    assert streaming["band"] == "strong"
    assert scorecard["hire_signal"] == "lean hire"  # mock debrief
    coverage = {r["requirement"]: r["status"] for r in scorecard["requirement_coverage"]}
    assert coverage["Kubernetes in production"] == "missing"
    # Study plan: cited, trust-filtered (SEO farm at 0.6 floor edge dropped
    # in favor of official docs ordering).
    assert scorecard["study_plan"], "study plan empty"
    assert scorecard["study_plan"][0]["url"].startswith("https://kubernetes.io")
    # Reveal: rubrics + evaluations now visible.
    fetched = client.get(f"/interviews/{sid}").json()
    assert all(q["final_evaluation"] is not None for q in fetched["questions"])


def test_job_scorecard_weak_competency_drives_study_plan() -> None:
    client = TestClient(server.app)
    session = _start_job_session(client)
    sid = session["session_id"]
    qids = [q["id"] for q in session["questions"]]
    # Skip everything → all competencies "not assessed" → study plan for all.
    last = None
    for qid in qids:
        last = client.post(
            f"/interviews/{sid}/answers", json={"question_id": qid, "skip": True}
        ).json()
    scorecard = last["summary"]["scorecard"]
    competencies_in_plan = {r["competency"] for r in scorecard["study_plan"]}
    assert "Cloud infrastructure" in competencies_in_plan
