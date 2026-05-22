import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import ClarificationQuestions
from pipeline.workers.clarification_questions_worker import (
    generate_clarification_questions,
)


class _MockMessages:
    def __init__(self, tool_input: dict) -> None:
        self.tool_input = tool_input
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="submit_clarification_questions",
                    input=self.tool_input,
                )
            ]
        )


class _MockClient:
    def __init__(self, tool_input: dict) -> None:
        self.messages = _MockMessages(tool_input)


def _sample_output() -> dict:
    return {
        "questions": [
            {
                "id": "scope",
                "question": "Which kind of databases?",
                "options": ["relational (SQL)", "NoSQL", "compare both"],
            },
            {
                "id": "angle",
                "question": "What angle do you want?",
                "options": ["fundamentals/explainer", "tutorial: build something"],
            },
            {
                "id": "must_cover",
                "question": "Anything specific you want covered? (optional)",
                "options": [],
            },
        ],
        "default_if_skipped": (
            "explainer covering relational fundamentals: ACID, indexes, "
            "transactions, query planner"
        ),
    }


def test_generate_clarification_questions_parses_tool_use() -> None:
    client = _MockClient(_sample_output())
    result = asyncio.run(
        generate_clarification_questions("database", "broad_undefined", client)
    )

    assert isinstance(result, ClarificationQuestions)
    assert len(result.questions) == 3
    assert result.questions[0].id == "scope"
    assert result.questions[-1].id == "must_cover"
    assert result.default_if_skipped.startswith("explainer covering")


def test_generate_clarification_questions_passes_topic_and_breadth_to_llm() -> None:
    client = _MockClient(_sample_output())
    asyncio.run(
        generate_clarification_questions(
            "Spring Boot", "broad_defined", client
        )
    )

    call = client.messages.calls[0]
    user_content = call["messages"][0]["content"]
    assert "topic: Spring Boot" in user_content
    assert "breadth: broad_defined" in user_content
    assert call["tool_choice"]["name"] == "submit_clarification_questions"


def test_generate_clarification_questions_must_cover_question_has_empty_options() -> None:
    """The free-text 'anything specific?' question must have empty options
    so frontends know to render a text input instead of a multi-select."""
    client = _MockClient(_sample_output())
    result = asyncio.run(
        generate_clarification_questions("database", "broad_undefined", client)
    )

    must_cover_q = next(q for q in result.questions if q.id == "must_cover")
    assert must_cover_q.options == []
