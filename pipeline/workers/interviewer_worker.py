"""Interview-question generation for practice mode.

Given a topic (and optionally an article the user just read), produce
free-text interview questions with a hidden rubric + model answer written
BEFORE any candidate answer exists. Real-world question patterns are pulled
via the existing multi-provider search so questions match what interviewers
actually ask, not just what the article happens to cover.
"""
from __future__ import annotations

import re

from pipeline.model_config import get_model
from pipeline.workers.citation_utils import scrub_dashes_in_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import InterviewQuestionSet
from pipeline.workers.search_worker import multi_search


_SYSTEM_PROMPT = load_prompt("interviewer_v1.txt")

_QUESTIONS_TOOL: dict = {
    "name": "submit_interview_questions",
    "description": "Submit the interview questions with rubrics and model answers.",
    "input_schema": InterviewQuestionSet.model_json_schema(),
}

# ~7k tokens of article — leaves ample room for prompt + output in every
# model's window. Articles longer than this are cut at a heading boundary.
_MAX_ARTICLE_CHARS = 28_000

_TRUNCATION_MARKER = (
    "\n\n[ARTICLE TRUNCATED — do not ask about content beyond this point]"
)

_HEADING_RE = re.compile(r"^#{1,3} .+$", re.MULTILINE)

# Cap on pattern snippets fed to the interviewer prompt.
_MAX_PATTERNS = 10


def _heading_outline(markdown: str) -> str:
    """The article's #/##/### headings, one per line, hash marks stripped."""
    return "\n".join(
        m.group(0).lstrip("#").strip() for m in _HEADING_RE.finditer(markdown)
    )


def _truncate_article(markdown: str, limit: int = _MAX_ARTICLE_CHARS) -> str:
    """Cut an over-long article at the last heading boundary before *limit*
    and append an explicit marker so the interviewer knows not to ask about
    the missing tail. Falls back to a hard cut when no heading precedes the
    limit."""
    if len(markdown) <= limit:
        return markdown
    cut = markdown.rfind("\n#", 0, limit)
    if cut <= 0:
        cut = limit
    return markdown[:cut].rstrip() + _TRUNCATION_MARKER


async def find_real_question_patterns(topic: str, level: str) -> list[str]:
    """Search the live web for interview questions actually asked on *topic*.

    Returns up to _MAX_PATTERNS "title — snippet" strings for the interviewer
    prompt. No search keys configured (or all providers erroring) degrades to
    an empty list — question generation works without patterns, just with
    less real-world grounding. No LLM call happens here.
    """
    queries = [
        f"{topic} interview questions",
        f"{topic} interview questions {level}",
        f"most commonly asked {topic} interview questions",
    ]
    try:
        results = await multi_search(queries)
    except Exception:
        return []
    patterns: list[str] = []
    for r in results:
        text = " — ".join(p for p in (r.title.strip(), r.snippet.strip()) if p)
        if text:
            patterns.append(text)
        if len(patterns) >= _MAX_PATTERNS:
            break
    return patterns


async def generate_interview_questions(
    *,
    topic: str,
    level: str,
    client,
    preset: str = "balanced",
    num_questions: int = 5,
    question_patterns: list[str] | None = None,
    article_markdown: str | None = None,
    verified_findings: list[str] | None = None,
    mode: str = "practice",
) -> InterviewQuestionSet:
    """One structured LLM call producing questions + rubrics + model answers.

    article_markdown=None → topic-only mode (questions grounded in the
    model's knowledge plus the searched question patterns).
    """
    patterns = question_patterns or []
    patterns_block = (
        "\n".join(f"- {p}" for p in patterns) if patterns else "none"
    )

    parts = [
        f"topic: {topic}",
        f"level: {level}",
        f"session_mode: {mode}",
        f"num_questions: {num_questions}",
        f"real_question_patterns:\n{patterns_block}",
    ]
    if article_markdown is not None:
        findings = verified_findings or []
        findings_block = (
            "\n".join(f"{i + 1}. {f}" for i, f in enumerate(findings))
            if findings
            else "none"
        )
        parts.append(f"article_outline:\n{_heading_outline(article_markdown)}")
        parts.append(f"verified_findings:\n{findings_block}")
        parts.append(f"article:\n{_truncate_article(article_markdown)}")
    user_content = "\n\n".join(parts)

    response = await client.messages.create(
        model=get_model("interviewer", preset),
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        tools=[_QUESTIONS_TOOL],
        tool_choice={"type": "tool", "name": "submit_interview_questions"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    question_set = scrub_dashes_in_model(
        InterviewQuestionSet.model_validate(tool_use.input))

    # Defensive post-validation: clamp to the requested count and re-id in
    # order so downstream code can rely on q1..qN regardless of model drift.
    questions = question_set.questions[:num_questions]
    for i, q in enumerate(questions):
        q.id = f"q{i + 1}"
    return InterviewQuestionSet(questions=questions)
