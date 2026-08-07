"""Grades a candidate's free-text answer against the question's hidden rubric.

The rubric and model answer were generated with the question, before any
candidate answer existed — the evaluator's only job is to compare against
that fixed bar. Supports a single follow-up round: when *followup_answer*
is passed, the call produces the FINAL combined evaluation of both answers.

Interviewer memory: the evaluator can receive a compact digest of the
session's PRIOR answers (interview_memory_digest). This is deliberately
application-layer state — we assemble exactly the context each grading
call should see from our own persisted session, instead of running one
ever-growing chat session whose history would drift the grading bar.
"""
from __future__ import annotations

from pipeline.model_config import get_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import AnswerEvaluation, InterviewQuestion
from pipeline.workers.interviewer_worker import _truncate_article


_SYSTEM_PROMPT = load_prompt("evaluator_v1.txt")
_DEBRIEF_PROMPT = load_prompt("debrief_v1.txt")

_EVAL_TOOL: dict = {
    "name": "submit_answer_evaluation",
    "description": "Submit the rubric-based evaluation of the candidate's answer.",
    "input_schema": AnswerEvaluation.model_json_schema(),
}

# Reference excerpt budget when the section anchor can't be located and we
# fall back to the (truncated) whole article.
_FALLBACK_CHARS = 12_000


def _section_excerpt(
    article_markdown: str, section_anchor: str, fallback_chars: int = _FALLBACK_CHARS
) -> str:
    """Slice the article from the anchor heading to the next heading of the
    same or higher level. Anchor missing → truncated whole article, so the
    evaluator always has *some* grounding text."""
    if section_anchor:
        lines = article_markdown.splitlines(keepends=True)
        start = None
        anchor_level = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("#"):
                continue
            level = len(stripped) - len(stripped.lstrip("#"))
            if stripped.lstrip("#").strip() == section_anchor:
                start, anchor_level = i, level
                break
        if start is not None:
            end = len(lines)
            for j in range(start + 1, len(lines)):
                stripped = lines[j].strip()
                if not stripped.startswith("#"):
                    continue
                if len(stripped) - len(stripped.lstrip("#")) <= anchor_level:
                    end = j
                    break
            return "".join(lines[start:end]).strip()
    return _truncate_article(article_markdown, fallback_chars)


# Memory digest budgets: enough for continuity, small enough that the
# fixed rubric stays the dominant signal in the prompt.
_MEMORY_MAX_QUESTIONS = 8
_MEMORY_FIELD_CHARS = 110


def _memory_trunc(text: str, limit: int = _MEMORY_FIELD_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def interview_memory_digest(session, exclude_question_id: str) -> str:
    """Deterministic one-line-per-question summary of every PRIOR resolved
    question: what was asked, how it scored, what the candidate demonstrated
    (the evaluator's quoted strengths) and where they fell short.

    Built from persisted session state, never from an LLM — so it costs
    ~40 tokens per prior question and cannot hallucinate history.
    """
    lines: list[str] = []
    for i, state in enumerate(session.questions, start=1):
        if state.question.id == exclude_question_id:
            continue
        if state.status == "skipped":
            lines.append(f"Q{i} (skipped): {_memory_trunc(state.question.question)}")
            continue
        if state.status != "completed":
            continue
        evaluation = state.followup.evaluation if state.followup else (
            state.first.evaluation if state.first else None
        )
        if evaluation is None:
            continue
        entry = (
            f"Q{i} ({state.final_score}/10, {evaluation.verdict}): "
            f"{_memory_trunc(state.question.question)}"
        )
        if evaluation.strengths:
            entry += f"\n   demonstrated: {_memory_trunc(evaluation.strengths[0])}"
        if evaluation.gaps:
            entry += f"\n   gap: {_memory_trunc(evaluation.gaps[0])}"
        lines.append(entry)
    return "\n".join(lines[-_MEMORY_MAX_QUESTIONS:])


async def evaluate_answer(
    *,
    question: InterviewQuestion,
    user_answer: str,
    level: str,
    client,
    preset: str = "balanced",
    article_markdown: str | None = None,
    job_context: str | None = None,
    session_memory: str | None = None,
    followup_question: str | None = None,
    followup_answer: str | None = None,
) -> AnswerEvaluation:
    # Job mode grounds grading in the JD/resume context instead of an
    # article excerpt.
    if job_context:
        reference = job_context
    else:
        reference = (
            _section_excerpt(article_markdown, question.section_anchor)
            if article_markdown
            else ""
        )
    rubric_block = "\n".join(f"- {p}" for p in question.rubric_key_points)

    parts = [
        f"question: {question.question}",
        f"difficulty: {level}",
        f"rubric_key_points:\n{rubric_block}",
        f"model_answer: {question.model_answer}",
        f"reference:\n{reference or 'none'}",
    ]
    if session_memory:
        parts.append(f"interview_so_far:\n{session_memory}")
    parts.append(f"candidate_answer: {user_answer}")
    if followup_answer is not None:
        parts.append(f"followup_question: {followup_question or ''}")
        parts.append(f"followup_answer: {followup_answer}")
    user_content = "\n\n".join(parts)

    response = await client.messages.create(
        model=get_model("evaluator", preset),
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        tools=[_EVAL_TOOL],
        tool_choice={"type": "tool", "name": "submit_answer_evaluation"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    evaluation = AnswerEvaluation.model_validate(tool_use.input)

    # Belt-and-braces: there is only ever ONE follow-up per question. Even if
    # the model ignores the combined-mode instruction, the server never loops.
    if followup_answer is not None and evaluation.needs_followup:
        evaluation = evaluation.model_copy(
            update={"needs_followup": False, "followup_question": ""}
        )
    return evaluation


async def generate_debrief(
    *,
    topic: str,
    level: str,
    results: list[dict],
    client,
    preset: str = "balanced",
) -> str:
    """Simulation-mode closing note: the interviewer's narrative verdict.

    *results* items: {question, score, verdict, gaps, strength}. Plain text
    call (no tool forcing); callers treat any failure as an empty debrief —
    the summary must never fail because the narrative garnish did.
    """
    lines = [f"topic: {topic}", f"level: {level}", "", "results:"]
    for i, r in enumerate(results, start=1):
        gaps = "; ".join(r.get("gaps", [])[:2]) or "none noted"
        strength = r.get("strength") or "none noted"
        lines.append(
            f"{i}. {r.get('question', '')}\n"
            f"   score: {r.get('score')}/10 ({r.get('verdict', '')})\n"
            f"   key gaps: {gaps}\n"
            f"   strongest moment: {strength}"
        )
    response = await client.messages.create(
        model=get_model("evaluator", preset),
        max_tokens=512,
        system=_DEBRIEF_PROMPT,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    text_block = next(
        (b for b in response.content if getattr(b, "type", "") == "text"), None
    )
    return text_block.text.strip() if text_block else ""
