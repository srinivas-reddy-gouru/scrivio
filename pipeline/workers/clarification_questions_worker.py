"""Generates structured clarification questions for broad topics.

Distinct from `clarification_worker.py`, which runs an interactive stdin loop
asking ONE question at a time. This module returns a structured multi-question
output in a single round-trip — suitable for the HTTP API where the frontend
collects all answers before resubmitting.
"""
from pathlib import Path
from typing import Literal

from pipeline.schemas.models import ClarificationQuestions


_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[1]
    / "prompts"
    / "clarification_questions_v1.txt"
).read_text(encoding="utf-8")


_QUESTIONS_TOOL: dict = {
    "name": "submit_clarification_questions",
    "description": (
        "Submit the structured clarification questions to ask the user "
        "before generating the article."
    ),
    "input_schema": ClarificationQuestions.model_json_schema(),
}


TopicBreadth = Literal["narrow", "broad_defined", "broad_undefined"]


async def generate_clarification_questions(
    topic: str,
    breadth: TopicBreadth,
    client,
) -> ClarificationQuestions:
    """Ask the LLM to generate 2-4 targeted clarification questions.

    Should only be called when `breadth` is "broad_defined" or "broad_undefined".
    Callers must NOT invoke this for narrow topics — the questions would feel
    nagging on an already-specific request.
    """
    user_content = f"topic: {topic}\nbreadth: {breadth}"

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        tools=[_QUESTIONS_TOOL],
        tool_choice={"type": "tool", "name": "submit_clarification_questions"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return ClarificationQuestions.model_validate(tool_use.input)
