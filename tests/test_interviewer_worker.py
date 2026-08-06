import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import InterviewQuestionSet
from pipeline.workers import interviewer_worker
from pipeline.workers.interviewer_worker import (
    _heading_outline,
    _truncate_article,
    find_real_question_patterns,
    generate_interview_questions,
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
                    name="submit_interview_questions",
                    input=self.tool_input,
                )
            ]
        )


class _MockClient:
    def __init__(self, tool_input: dict) -> None:
        self.messages = _MockMessages(tool_input)


def _sample_questions(n: int = 2) -> dict:
    return {
        "questions": [
            {
                "id": f"placeholder{i}",
                "question": f"Explain concept number {i} in depth.",
                "difficulty": "intermediate",
                "section_anchor": "",
                "rubric_key_points": ["defines it", "explains why", "example"],
                "model_answer": "A strong answer defines the concept clearly.",
            }
            for i in range(n)
        ]
    }


def test_generate_questions_parses_and_reids() -> None:
    client = _MockClient(_sample_questions(3))
    result = asyncio.run(
        generate_interview_questions(
            topic="Kafka",
            level="intermediate",
            client=client,
            num_questions=2,
        )
    )
    assert isinstance(result, InterviewQuestionSet)
    # Clamped to num_questions and re-id'd q1..qN regardless of model output.
    assert [q.id for q in result.questions] == ["q1", "q2"]


def test_prompt_includes_patterns_findings_and_count() -> None:
    client = _MockClient(_sample_questions())
    asyncio.run(
        generate_interview_questions(
            topic="Kafka",
            level="advanced",
            client=client,
            num_questions=4,
            question_patterns=["What is a consumer group? — asked at BigCo"],
            article_markdown="# Kafka\n\n## Consumer groups\n\nBody.",
            verified_findings=["Rebalancing pauses consumption"],
        )
    )
    sent = client.messages.calls[0]["messages"][-1]["content"]
    assert "num_questions: 4" in sent
    assert "What is a consumer group? — asked at BigCo" in sent
    assert "Rebalancing pauses consumption" in sent
    assert "article_outline:" in sent
    assert "Consumer groups" in sent


def test_topic_only_prompt_has_no_article_blocks() -> None:
    client = _MockClient(_sample_questions())
    asyncio.run(
        generate_interview_questions(
            topic="Kafka", level="basic", client=client
        )
    )
    sent = client.messages.calls[0]["messages"][-1]["content"]
    assert "article_outline:" not in sent
    assert "verified_findings:" not in sent
    assert "real_question_patterns:\nnone" in sent


def test_truncate_article_cuts_at_heading_with_marker() -> None:
    long_article = "# Title\n\n" + ("word " * 3000) + "\n## Tail section\n\n" + (
        "tail " * 8000
    )
    truncated = _truncate_article(long_article, limit=20_000)
    assert len(truncated) < len(long_article)
    assert truncated.endswith(
        "[ARTICLE TRUNCATED — do not ask about content beyond this point]"
    )
    # The prompt still carries the full outline even when the body is cut.
    assert "Tail section" in _heading_outline(long_article)


def test_truncate_article_short_input_untouched() -> None:
    md = "# Short\n\nBody."
    assert _truncate_article(md) == md


def test_find_patterns_returns_snippets(monkeypatch) -> None:
    async def fake_search(queries, **kwargs):
        return [
            SimpleNamespace(title="Top Kafka questions", snippet="What is an ISR?"),
            SimpleNamespace(title="", snippet=""),
        ]

    monkeypatch.setattr(interviewer_worker, "multi_search", fake_search)
    patterns = asyncio.run(find_real_question_patterns("Kafka", "advanced"))
    assert patterns == ["Top Kafka questions — What is an ISR?"]


def test_find_patterns_degrades_to_empty_on_error(monkeypatch) -> None:
    async def broken_search(queries, **kwargs):
        raise RuntimeError("no providers")

    monkeypatch.setattr(interviewer_worker, "multi_search", broken_search)
    assert asyncio.run(find_real_question_patterns("Kafka", "basic")) == []
