import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import server
from main import MockAnthropicClient
from pipeline.schemas.models import (
    AnswerEvaluation,
    InterviewQuestionSet,
    InterviewSession,
)


# A long answer (>= 8 words) grades "strong" in the mock; a short one
# grades "shallow" and triggers the one-follow-up flow.
STRONG_ANSWER = (
    "Kafka consumer groups let multiple consumers share partitions so that "
    "each message is processed exactly once per group."
)
SHORT_ANSWER = "It shares partitions."


@pytest.fixture(autouse=True)
def _isolate_output_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(server, "OUTPUT_ROOT", Path(td))
        # Locks are keyed by session id; sessions are unique per test run,
        # but clear anyway so the dict doesn't grow across tests.
        server._interview_locks.clear()
        yield


@pytest.fixture(autouse=True)
def _mock_anthropic_for_api(monkeypatch):
    monkeypatch.setattr(
        server, "_anthropic_client", lambda request: MockAnthropicClient(request)
    )


@pytest.fixture(autouse=True)
def _no_web_search(monkeypatch):
    """Keep tests hermetic: pattern search must never hit real providers
    (search keys may be present in the developer's .env)."""
    async def fake_patterns(topic, level):
        return [f"What is {topic}? — commonly asked"]

    monkeypatch.setattr(server, "find_real_question_patterns", fake_patterns)


def _make_article_dir(article_id: str = "20260101-000000__kafka__abcd1234") -> str:
    article_dir = server.OUTPUT_ROOT / article_id
    article_dir.mkdir(parents=True)
    (article_dir / "intermediate.md").write_text(
        "# Kafka\n\n## Why it matters\n\nKafka decouples producers from "
        "consumers.\n\n## Consumer groups\n\nRebalancing details.\n",
        encoding="utf-8",
    )
    meta = {
        "job_id": "test",
        "generated_at": "2026-01-01T00:00:00",
        "request": {"topic": "Kafka", "explanation_level": "intermediate"},
        "verification_reports": [
            {"claim_id": "c1", "support_status": "supported",
             "verifier_note": "Kafka retains messages for a configurable period"},
            {"claim_id": "c2", "support_status": "unsupported",
             "verifier_note": "Wrong claim"},
        ],
    }
    (article_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return article_id


def _create_session(client: TestClient, **body) -> dict:
    response = client.post("/interviews", json={"num_questions": 3, **body})
    assert response.status_code == 200, response.text
    return response.json()


# ── Session creation ─────────────────────────────────────────────────

def test_create_topic_session_hides_rubric_and_persists_it() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")

    assert session["article_id"] is None
    assert session["complete"] is False
    assert len(session["questions"]) > 0
    for q in session["questions"]:
        assert q["status"] == "pending"
        assert q["model_answer"] is None        # hidden while open
        assert q["rubric_key_points"] is None   # hidden while open

    # On disk the rubric exists from the start.
    path = server.OUTPUT_ROOT / "interviews" / f"{session['session_id']}.json"
    stored = InterviewSession.model_validate_json(path.read_text())
    assert all(s.question.rubric_key_points for s in stored.questions)
    assert all(s.question.model_answer for s in stored.questions)


def test_create_article_session_grounds_in_article() -> None:
    article_id = _make_article_dir()
    client = TestClient(server.app)
    session = _create_session(client, article_id=article_id)
    assert session["article_id"] == article_id
    assert session["topic"] == "Kafka"
    assert session["level"] == "intermediate"
    # Mock anchors the first question to the first outline heading.
    assert session["questions"][0]["section_anchor"] == "Why it matters"


def test_create_article_session_without_verification_reports() -> None:
    article_id = _make_article_dir()
    meta_path = server.OUTPUT_ROOT / article_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    del meta["verification_reports"]
    meta_path.write_text(json.dumps(meta))

    client = TestClient(server.app)
    session = _create_session(client, article_id=article_id)
    assert len(session["questions"]) > 0


def test_create_rejects_topic_and_article_both_or_neither() -> None:
    client = TestClient(server.app)
    assert client.post("/interviews", json={}).status_code == 422
    article_id = _make_article_dir()
    both = client.post(
        "/interviews", json={"topic": "Kafka", "article_id": article_id}
    )
    assert both.status_code == 422


def test_create_unknown_or_traversal_article_id_404s() -> None:
    client = TestClient(server.app)
    assert client.post(
        "/interviews", json={"article_id": "does-not-exist"}
    ).status_code == 404
    assert client.post(
        "/interviews", json={"article_id": "../../etc/passwd"}
    ).status_code == 404


def test_create_missing_level_file_400s() -> None:
    article_id = _make_article_dir()
    client = TestClient(server.app)
    response = client.post(
        "/interviews", json={"article_id": article_id, "level": "advanced"}
    )
    assert response.status_code == 400


# ── Answer flow ──────────────────────────────────────────────────────

def test_strong_answer_completes_and_reveals_model_answer() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    qid = session["questions"][0]["id"]

    response = client.post(
        f"/interviews/{session['session_id']}/answers",
        json={"question_id": qid, "answer": STRONG_ANSWER},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluation"]["verdict"] == "strong"
    assert body["followup_question"] is None
    assert body["question"]["status"] == "completed"
    assert body["question"]["model_answer"]          # revealed on completion
    assert body["question"]["final_score"] == 8


def test_shallow_answer_triggers_single_followup_flow() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    sid = session["session_id"]
    qid = session["questions"][0]["id"]

    first = client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qid, "answer": SHORT_ANSWER},
    ).json()
    assert first["question"]["status"] == "awaiting_followup"
    assert first["followup_question"]
    assert first["question"]["model_answer"] is None  # still hidden mid-question

    second = client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qid, "answer": "A consumer joining or leaving the group."},
    ).json()
    assert second["question"]["status"] == "completed"
    assert second["followup_question"] is None
    assert second["question"]["final_score"] == 7     # combined evaluation

    # Both rounds persisted.
    path = server.OUTPUT_ROOT / "interviews" / f"{sid}.json"
    stored = InterviewSession.model_validate_json(path.read_text())
    state = next(s for s in stored.questions if s.question.id == qid)
    assert state.first is not None and state.followup is not None
    assert state.first.answer == SHORT_ANSWER


def test_skip_records_no_evaluation() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    sid = session["session_id"]
    qid = session["questions"][0]["id"]

    response = client.post(
        f"/interviews/{sid}/answers", json={"question_id": qid, "skip": True}
    ).json()
    assert response["question"]["status"] == "skipped"
    assert response["evaluation"] is None
    assert response["question"]["model_answer"]       # revealed for learning


def test_reanswering_completed_question_409s() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    sid = session["session_id"]
    qid = session["questions"][0]["id"]
    client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qid, "answer": STRONG_ANSWER},
    )
    again = client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qid, "answer": STRONG_ANSWER},
    )
    assert again.status_code == 409


def test_empty_answer_422_and_unknown_question_404() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    sid = session["session_id"]
    qid = session["questions"][0]["id"]
    assert client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qid, "answer": "   "},
    ).status_code == 422
    assert client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": "q999", "answer": STRONG_ANSWER},
    ).status_code == 404


def test_completing_all_questions_yields_summary() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    sid = session["session_id"]
    qids = [q["id"] for q in session["questions"]]

    client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qids[0], "answer": STRONG_ANSWER},
    )
    last = None
    for qid in qids[1:]:
        last = client.post(
            f"/interviews/{sid}/answers", json={"question_id": qid, "skip": True}
        ).json()

    assert last["session_complete"] is True
    summary = last["summary"]
    assert summary["total_questions"] == len(qids)
    assert summary["answered"] == 1
    assert summary["skipped"] == len(qids) - 1
    assert summary["average_score"] == 8.0            # skips excluded

    fetched = client.get(f"/interviews/{sid}").json()
    assert fetched["complete"] is True
    assert fetched["summary"]["average_score"] == 8.0


# ── History endpoints ────────────────────────────────────────────────

def test_list_filters_by_article_and_skips_corrupt() -> None:
    client = TestClient(server.app)
    article_id = _make_article_dir()
    topic_session = _create_session(client, topic="Kafka")
    article_session = _create_session(client, article_id=article_id)

    corrupt = server.OUTPUT_ROOT / "interviews" / "20260101-000000-badbad.json"
    corrupt.write_text("{not json", encoding="utf-8")

    everything = client.get("/interviews").json()
    ids = {s["session_id"] for s in everything}
    assert ids == {topic_session["session_id"], article_session["session_id"]}

    filtered = client.get(f"/interviews?article_id={article_id}").json()
    assert [s["session_id"] for s in filtered] == [article_session["session_id"]]


def test_get_corrupt_session_410_unknown_404() -> None:
    client = TestClient(server.app)
    interviews = server.OUTPUT_ROOT / "interviews"
    interviews.mkdir(parents=True)
    (interviews / "20260101-000000-badbad.json").write_text("{not json")
    assert client.get("/interviews/20260101-000000-badbad").status_code == 410
    assert client.get("/interviews/nope").status_code == 404
    assert client.get("/interviews/..%2Fescape").status_code == 404


# ── Session modes ────────────────────────────────────────────────────

def test_simulation_hides_feedback_until_complete() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka", mode="simulation")
    assert session["mode"] == "simulation"
    sid = session["session_id"]
    qids = [q["id"] for q in session["questions"]]

    # Mid-session: even a SHORT answer gets no evaluation, no follow-up
    # (follow-ups are practice-only), and the question stays redacted.
    first = client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qids[0], "answer": SHORT_ANSWER},
    ).json()
    assert first["evaluation"] is None
    assert first["followup_question"] is None
    assert first["question"]["status"] == "completed"
    assert first["question"]["final_evaluation"] is None
    assert first["question"]["model_answer"] is None

    # Completing the screen reveals everything + the debrief.
    last = None
    for qid in qids[1:]:
        last = client.post(
            f"/interviews/{sid}/answers",
            json={"question_id": qid, "answer": STRONG_ANSWER},
        ).json()
    assert last["session_complete"] is True
    assert last["evaluation"] is not None            # reveal on completion
    assert last["summary"]["debrief"]                # interviewer's notes
    fetched = client.get(f"/interviews/{sid}").json()
    assert all(q["final_evaluation"] is not None for q in fetched["questions"])
    assert all(q["model_answer"] for q in fetched["questions"])


def test_drill_mode_no_followups_and_mode_stored() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka", mode="drill")
    sid = session["session_id"]
    qid = session["questions"][0]["id"]
    # Short answer would trigger a follow-up in practice mode; not in drill.
    response = client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qid, "answer": SHORT_ANSWER},
    ).json()
    assert response["followup_question"] is None
    assert response["question"]["status"] == "completed"
    assert response["evaluation"] is not None        # drill shows feedback

    stored = InterviewSession.model_validate_json(
        (server.OUTPUT_ROOT / "interviews" / f"{sid}.json").read_text()
    )
    assert stored.mode == "drill"


def test_predicted_score_persists_and_calibration_computed() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    sid = session["session_id"]
    qids = [q["id"] for q in session["questions"]]

    # Strong answer (mock scores 8) with prediction 5 → delta 3.
    client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qids[0], "answer": STRONG_ANSWER, "predicted_score": 5},
    )
    last = None
    for qid in qids[1:]:
        last = client.post(
            f"/interviews/{sid}/answers",
            json={"question_id": qid, "answer": STRONG_ANSWER, "predicted_score": 8},
        ).json()
    # deltas: |5-8|=3 and |8-8|=0 → mean 1.5
    assert last["summary"]["calibration_gap"] == 1.5
    assert last["question"]["predicted_score"] == 8

    stats = client.get("/interviews/stats").json()
    assert stats["calibration_gap"] == 1.5


def test_old_session_files_without_mode_still_load() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    path = server.OUTPUT_ROOT / "interviews" / f"{session['session_id']}.json"
    data = json.loads(path.read_text())
    del data["mode"]  # simulate a pre-mode session file
    path.write_text(json.dumps(data, default=str))
    fetched = client.get(f"/interviews/{session['session_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["mode"] == "practice"


# ── Streaks, mastery, badges ─────────────────────────────────────────

def _complete_session(client: TestClient, topic: str, mode: str = "practice") -> None:
    session = _create_session(client, topic=topic, mode=mode)
    for q in session["questions"]:
        client.post(
            f"/interviews/{session['session_id']}/answers",
            json={"question_id": q["id"], "answer": STRONG_ANSWER},
        )


def test_streak_and_badges() -> None:
    client = TestClient(server.app)
    _complete_session(client, "Kafka")
    stats = client.get("/interviews/stats").json()
    assert stats["streak_days"] == 1        # completed today
    badges = {b["id"]: b["earned"] for b in stats["badges"]}
    assert badges["first-session"] is True
    assert badges["first-strong"] is True   # mock strong answer scores 8
    assert badges["five-sessions"] is False
    assert badges["streak-3"] is False

    topics = {t["topic"]: t for t in stats["per_topic"]}
    assert topics["Kafka"]["mastery"] == 80  # avg 8.0 → 80


def test_drill_sergeant_badge() -> None:
    client = TestClient(server.app)
    for _ in range(3):
        _complete_session(client, "Kafka", mode="drill")
    stats = client.get("/interviews/stats").json()
    badges = {b["id"]: b["earned"] for b in stats["badges"]}
    assert badges["drill-sergeant"] is True


def test_streak_computation_edges() -> None:
    from datetime import date, timedelta
    today = date.today()
    # Practiced today and the two days before → 3-day streak.
    assert server._compute_streak({today, today - timedelta(days=1), today - timedelta(days=2)}) == 3
    # Practiced yesterday only → streak survives overnight.
    assert server._compute_streak({today - timedelta(days=1)}) == 1
    # Gap breaks the streak.
    assert server._compute_streak({today, today - timedelta(days=2)}) == 1
    # Nothing recent → zero.
    assert server._compute_streak({today - timedelta(days=3)}) == 0
    assert server._compute_streak(set()) == 0


# ── Mock/tool-name coupling regression guard ─────────────────────────

def test_mock_tool_inputs_validate_against_models() -> None:
    from pipeline.schemas.models import ArticleRequest

    mock = MockAnthropicClient(ArticleRequest(topic="Kafka")).messages
    questions = mock._mock_tool_input(
        "submit_interview_questions", "level: advanced"
    )
    parsed = InterviewQuestionSet.model_validate(questions)
    assert all(q.difficulty == "advanced" for q in parsed.questions)

    for content in (
        f"candidate_answer: {STRONG_ANSWER}",
        f"candidate_answer: {SHORT_ANSWER}",
        "candidate_answer: x\n\nfollowup_answer: y",
    ):
        AnswerEvaluation.model_validate(
            mock._mock_tool_input("submit_answer_evaluation", content)
        )


# ── Stats endpoint ───────────────────────────────────────────────────

def test_stats_empty_dir_returns_zeros() -> None:
    client = TestClient(server.app)
    stats = client.get("/interviews/stats").json()
    assert stats["total_sessions"] == 0
    assert stats["completed_sessions"] == 0
    assert stats["average_score"] is None
    assert stats["recent_scores"] == []


def test_stats_not_shadowed_by_session_route() -> None:
    """/interviews/stats must not be captured as session_id='stats'."""
    client = TestClient(server.app)
    response = client.get("/interviews/stats")
    assert response.status_code == 200
    assert "total_sessions" in response.json()


def test_stats_aggregates_completed_sessions() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    sid = session["session_id"]
    qids = [q["id"] for q in session["questions"]]
    # One strong answer (score 8 in mock), skip the rest → session complete.
    client.post(
        f"/interviews/{sid}/answers",
        json={"question_id": qids[0], "answer": STRONG_ANSWER},
    )
    for qid in qids[1:]:
        client.post(
            f"/interviews/{sid}/answers", json={"question_id": qid, "skip": True}
        )
    # A second, unfinished session on another topic.
    _create_session(client, topic="Redis")

    stats = client.get("/interviews/stats").json()
    assert stats["total_sessions"] == 2
    assert stats["completed_sessions"] == 1
    assert stats["total_answered"] == 1
    assert stats["average_score"] == 8.0
    assert stats["recent_scores"] == [8.0]
    topics = {t["topic"] for t in stats["per_topic"]}
    assert "Kafka" in topics


def test_stats_skips_corrupt_files() -> None:
    client = TestClient(server.app)
    _create_session(client, topic="Kafka")
    (server.OUTPUT_ROOT / "interviews" / "20260101-000000-broken.json").write_text(
        "{not json", encoding="utf-8"
    )
    stats = client.get("/interviews/stats").json()
    assert stats["total_sessions"] == 1


def test_interview_session_json_roundtrip() -> None:
    client = TestClient(server.app)
    session = _create_session(client, topic="Kafka")
    path = server.OUTPUT_ROOT / "interviews" / f"{session['session_id']}.json"
    stored = InterviewSession.model_validate_json(path.read_text())
    assert stored.model_dump() == InterviewSession.model_validate_json(
        stored.model_dump_json()
    ).model_dump()
