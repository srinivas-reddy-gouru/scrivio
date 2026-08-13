"""The coding round: static checks that never execute, and a sealed bar.

The premise being tested is that this round grades an interview, not a
submission. The clarify and approach phases exist because candidates fail
for not asking and not costing their solution, and the code phase is
judged against facts obtained by parsing rather than the candidate's own
account of what they wrote.
"""
from fastapi.testclient import TestClient

from api import server
from pipeline.workers.coding_round_worker import (
    PHASE_ORDER, checks_block, static_code_checks,
)

SIG = "def merge(intervals: list[list[int]]) -> list[list[int]]:"

GOOD = """
def merge(intervals):
    out = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out
"""


def _check(checks, name):
    return next(c for c in checks if c.name == name)


# ── Static checks: parsing only, never execution ─────────────────────

def test_a_working_solution_passes_every_check():
    checks = static_code_checks(GOOD, SIG)
    assert _check(checks, "parses").passed
    assert _check(checks, "signature").passed
    assert _check(checks, "returns").passed
    assert "1" in _check(checks, "loop_depth").detail


def test_syntax_error_is_caught_and_stops_further_claims():
    """A grader told only "the candidate says it works" would believe it."""
    checks = static_code_checks("def merge(intervals)\n    return []", SIG)
    assert not _check(checks, "parses").passed
    assert "line 1" in _check(checks, "parses").detail
    assert len(checks) == 1, "nothing else is knowable once it does not parse"


def test_wrong_function_name_is_caught():
    checks = static_code_checks("def solve(x):\n    return x\n", SIG)
    assert not _check(checks, "signature").passed
    assert "merge" in _check(checks, "signature").detail


def test_stub_and_missing_return_are_caught():
    checks = static_code_checks("def merge(intervals):\n    pass\n", SIG)
    assert not _check(checks, "returns").passed
    assert not _check(checks, "stub").passed


def test_empty_submission_is_a_finding_not_a_crash():
    checks = static_code_checks("   ", SIG)
    assert checks and not checks[0].passed


def test_non_python_is_not_pretend_parsed():
    """Claiming to have checked Java syntax with a Python parser would be
    a lie dressed as a finding."""
    checks = static_code_checks("class X { }", "public int[][] merge(...)", language="java")
    assert _check(checks, "parses").passed
    assert "not parsed here" in _check(checks, "parses").detail


def test_checks_block_marks_failures_for_the_grader():
    rendered = checks_block(static_code_checks("def merge(intervals)\n", SIG))
    assert "FAIL" in rendered


def test_static_checks_never_execute_submitted_code():
    """The one property that must never regress: parsing a payload that
    would delete a file must not delete the file."""
    import tempfile, os
    fd, path = tempfile.mkstemp()
    os.close(fd)
    payload = f"import os\ndef merge(intervals):\n    os.remove({path!r})\n    return []\n"
    static_code_checks(payload, SIG)
    assert os.path.exists(path), "static checks must parse, never execute"
    os.unlink(path)


# ── The round through the API ────────────────────────────────────────

def test_coding_session_seals_the_problem_until_the_round_ends():
    client = TestClient(server.app)
    r = client.post("/interviews", json={
        "topic": "arrays and intervals", "mode": "coding", "level": "intermediate",
    })
    assert r.status_code == 200, r.text
    session = r.json()
    assert session["mode"] == "coding"
    assert len(session["questions"]) == len(PHASE_ORDER)

    # The statement and signature are needed to work; the answers are not.
    problem = session["coding_problem"]
    assert problem["statement"] and problem["signature"], "the work must be visible"
    assert problem["unstated_constraints"] is None, "clarify answers must stay sealed"
    assert problem["optimal_complexity"] is None, "approach answer must stay sealed"
    assert problem["model_solution"] is None, "reference solution must stay sealed"
    for q in session["questions"]:
        assert q.get("rubric_key_points") in (None, []), "rubric must stay sealed"


def test_coding_round_requires_a_topic():
    client = TestClient(server.app)
    assert client.post("/interviews", json={"mode": "coding"}).status_code == 422


def test_code_phase_is_graded_with_static_findings(monkeypatch):
    """The code phase must reach the grader with parse findings attached,
    so a broken submission cannot be described into a passing grade."""
    seen = {}
    real = server.evaluate_answer

    async def spy(**kwargs):
        seen.update(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(server, "evaluate_answer", spy)
    client = TestClient(server.app)
    sid = client.post("/interviews", json={
        "topic": "arrays", "mode": "coding"}).json()["session_id"]

    for qid, answer in [("q1", "Is the input sorted?"),
                        ("q2", "Sort then sweep, O(n log n)."),
                        ("q3", "def merge(intervals)\n  return []")]:
        client.post(f"/interviews/{sid}/answers",
                    json={"question_id": qid, "answer": answer})

    assert seen.get("code_checks"), "code phase must pass static findings"
    assert "FAIL" in seen["code_checks"], "the broken submission must be reported"


def test_interview_text_is_stripped_of_dashes_deterministically():
    """The product's no-dash rule reached resumes and articles but never
    the three interview prompts, so questions and follow-ups arrived with
    em dashes in them. Prompt text is the reminder; this is the guard."""
    from pipeline.schemas.models import AnswerEvaluation
    from pipeline.workers.citation_utils import scrub_dashes_in_model

    ev = scrub_dashes_in_model(AnswerEvaluation(
        score=7, verdict="adequate",
        strengths=["You named the sweep — clearly"],
        gaps=["The cost — unstated"], misconceptions=[], suggestions=[],
        section_pointers=[], needs_followup=True,
        followup_question="What about n — and the range?",
    ))
    assert "—" not in ev.followup_question
    assert not any("—" in s for s in ev.strengths + ev.gaps)
