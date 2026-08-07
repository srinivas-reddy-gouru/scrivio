"""Interviewer memory: the deterministic prior-answer digest and its flow
into evaluate_answer. Memory informs the evaluator's words; the score must
still come from the fixed rubric — so the digest is compact, factual, and
built from persisted state, never an LLM."""
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import server
from main import MockAnthropicClient
from pipeline.schemas.models import (
    AnswerEvaluation,
    InterviewAnswerRecord,
    InterviewQuestion,
    InterviewQuestionState,
    InterviewSession,
)
from pipeline.workers.answer_evaluator_worker import (
    evaluate_answer,
    interview_memory_digest,
)


STRONG_ANSWER = (
    "Kafka consumer groups let multiple consumers share partitions so that "
    "each message is processed exactly once per group."
)
SHORT_ANSWER = "It shares partitions."


def _question(qid: str, text: str) -> InterviewQuestion:
    return InterviewQuestion(
        id=qid, question=text, difficulty="intermediate",
        rubric_key_points=["first point", "second point"],
        model_answer="ideal", section_anchor="",
    )


def _evaluation(**overrides) -> AnswerEvaluation:
    base = dict(
        score=8, verdict="strong",
        strengths=["You correctly explained partition sharing across consumers."],
        gaps=["The rebalancing protocol was not mentioned."],
        misconceptions=[], suggestions=["s"], section_pointers=[],
        needs_followup=False, followup_question="",
    )
    base.update(overrides)
    return AnswerEvaluation(**base)


def _session_with_history() -> InterviewSession:
    q1 = InterviewQuestionState(
        question=_question("q1", "What is a consumer group?"),
        status="completed", final_score=8,
        first=InterviewAnswerRecord(answer="a1", evaluation=_evaluation()),
    )
    q2 = InterviewQuestionState(
        question=_question("q2", "Explain rebalancing."),
        status="skipped",
    )
    q3 = InterviewQuestionState(
        question=_question("q3", "Describe exactly-once delivery."),
        status="pending",
    )
    return InterviewSession(
        session_id="s1", article_id=None, topic="kafka",
        level="intermediate", questions=[q1, q2, q3],
    )


# ── Digest builder ───────────────────────────────────────────────────

def test_digest_summarizes_completed_and_skipped():
    digest = interview_memory_digest(_session_with_history(), "q3")
    assert "Q1 (8/10, strong): What is a consumer group?" in digest
    assert "demonstrated: You correctly explained partition sharing" in digest
    assert "gap: The rebalancing protocol was not mentioned." in digest
    assert "Q2 (skipped): Explain rebalancing." in digest
    assert "exactly-once" not in digest  # pending questions have no history


def test_digest_excludes_the_question_being_graded():
    # Grading q1's follow-up: q1 itself must not appear as "memory".
    digest = interview_memory_digest(_session_with_history(), "q1")
    assert "consumer group" not in digest
    assert "Q2 (skipped)" in digest


def test_digest_empty_for_first_question():
    session = _session_with_history()
    for state in session.questions:
        state.status = "pending"
        state.first = None
        state.final_score = None
    assert interview_memory_digest(session, "q1") == ""


def test_digest_prefers_final_followup_evaluation():
    session = _session_with_history()
    session.questions[0].followup = InterviewAnswerRecord(
        answer="better answer",
        evaluation=_evaluation(score=7, verdict="adequate",
                               strengths=["You then named the group coordinator."]),
    )
    session.questions[0].final_score = 7
    digest = interview_memory_digest(session, "q3")
    assert "(7/10, adequate)" in digest
    assert "group coordinator" in digest


def test_digest_truncates_and_caps():
    session = _session_with_history()
    long_text = "word " * 100
    states = []
    for i in range(12):
        states.append(InterviewQuestionState(
            question=_question(f"q{i}", long_text),
            status="completed", final_score=5,
            first=InterviewAnswerRecord(
                answer="a", evaluation=_evaluation(strengths=[long_text], gaps=[]),
            ),
        ))
    session.questions = states
    digest = interview_memory_digest(session, "none")
    assert digest.count("Q") <= 8 * 2  # ≤ 8 entries (Q label + none in text)
    assert "Q1 " not in digest and "Q12" in digest  # keeps the most recent
    for line in digest.splitlines():
        assert len(line) < 140  # every field truncated


# ── evaluate_answer threading ────────────────────────────────────────

class _CapturingClient:
    def __init__(self):
        self.user_content = None
        self.messages = self

    async def create(self, **kwargs):
        self.user_content = kwargs["messages"][-1]["content"]
        return SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", name="submit_answer_evaluation",
            input=_evaluation().model_dump(),
        )])


def test_evaluate_answer_includes_memory_block():
    import asyncio

    client = _CapturingClient()
    asyncio.run(evaluate_answer(
        question=_question("q3", "Describe exactly-once delivery."),
        user_answer=STRONG_ANSWER, level="intermediate", client=client,
        session_memory="Q1 (8/10, strong): What is a consumer group?",
    ))
    assert "interview_so_far:\nQ1 (8/10, strong)" in client.user_content
    # Memory sits before the answer so the rubric/answer stay adjacent.
    assert client.user_content.index("interview_so_far:") < client.user_content.index(
        "candidate_answer:"
    )


def test_evaluate_answer_omits_block_without_memory():
    import asyncio

    client = _CapturingClient()
    asyncio.run(evaluate_answer(
        question=_question("q1", "What is a consumer group?"),
        user_answer=STRONG_ANSWER, level="intermediate", client=client,
    ))
    assert "interview_so_far" not in client.user_content


# ── API flow ─────────────────────────────────────────────────────────

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
    async def fake_patterns(topic, level):
        return [f"What is {topic}? — commonly asked"]

    monkeypatch.setattr(server, "find_real_question_patterns", fake_patterns)


@pytest.fixture
def _capture_memory(monkeypatch):
    """Wrap server.evaluate_answer to record the session_memory each grading
    call receives, while still producing real (mock-backed) evaluations."""
    captured = []
    real = server.evaluate_answer

    async def wrapper(**kwargs):
        captured.append(kwargs.get("session_memory"))
        return await real(**kwargs)

    monkeypatch.setattr(server, "evaluate_answer", wrapper)
    return captured


def _create_session(client: TestClient, mode: str) -> dict:
    response = client.post("/interviews", json={
        "topic": "kafka", "level": "intermediate", "mode": mode,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_memory_flows_across_practice_session(_capture_memory):
    client = TestClient(server.app)
    session = _create_session(client, "practice")
    sid = session["session_id"]
    q1, q2 = [q["id"] for q in session["questions"]]

    client.post(f"/interviews/{sid}/answers",
                json={"question_id": q1, "answer": STRONG_ANSWER})
    client.post(f"/interviews/{sid}/answers",
                json={"question_id": q2, "answer": STRONG_ANSWER})

    assert _capture_memory[0] is None            # first question: no history
    assert _capture_memory[1] is not None        # second question remembers q1
    assert "Q1 (8/10, strong)" in _capture_memory[1]
    assert "You correctly identified the core concept." in _capture_memory[1]


def test_followup_round_carries_memory_but_not_own_question(_capture_memory):
    client = TestClient(server.app)
    session = _create_session(client, "practice")
    sid = session["session_id"]
    q1, q2 = [q["id"] for q in session["questions"]]

    # Complete q1, then give q2 a short answer → follow-up → final answer.
    client.post(f"/interviews/{sid}/answers",
                json={"question_id": q1, "answer": STRONG_ANSWER})
    r = client.post(f"/interviews/{sid}/answers",
                    json={"question_id": q2, "answer": SHORT_ANSWER})
    assert r.json()["question"]["status"] == "awaiting_followup"
    client.post(f"/interviews/{sid}/answers",
                json={"question_id": q2, "answer": STRONG_ANSWER})

    followup_memory = _capture_memory[2]
    assert followup_memory is not None
    assert "Q1 (8/10, strong)" in followup_memory
    # q2 is mid-grading — it must not appear in its own memory.
    assert "Q2" not in followup_memory


def test_drill_mode_stays_memoryless(_capture_memory):
    client = TestClient(server.app)
    session = _create_session(client, "drill")
    sid = session["session_id"]
    for q in session["questions"]:
        client.post(f"/interviews/{sid}/answers",
                    json={"question_id": q["id"], "answer": STRONG_ANSWER})
    assert _capture_memory and all(m is None for m in _capture_memory)
