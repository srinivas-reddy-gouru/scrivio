import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import AnswerEvaluation, InterviewQuestion
from pipeline.workers.answer_evaluator_worker import (
    _section_excerpt,
    evaluate_answer,
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
                    name="submit_answer_evaluation",
                    input=self.tool_input,
                )
            ]
        )


class _MockClient:
    def __init__(self, tool_input: dict) -> None:
        self.messages = _MockMessages(tool_input)


def _question() -> InterviewQuestion:
    return InterviewQuestion(
        id="q1",
        question="Explain consumer group rebalancing.",
        difficulty="intermediate",
        section_anchor="Consumer groups",
        rubric_key_points=["what rebalancing is", "when it triggers"],
        model_answer="Rebalancing redistributes partitions across consumers.",
    )


def _evaluation(**overrides) -> dict:
    base = {
        "score": 7,
        "verdict": "adequate",
        "strengths": ["Mentioned partition redistribution"],
        "gaps": ["Trigger conditions missing"],
        "misconceptions": [],
        "suggestions": ["Re-read 'Consumer groups'"],
        "section_pointers": ["Consumer groups"],
        "needs_followup": False,
        "followup_question": "",
    }
    base.update(overrides)
    return base


_ARTICLE = (
    "# Kafka\n\nIntro.\n\n## Consumer groups\n\nRebalancing details here.\n\n"
    "## Replication\n\nOther content.\n"
)


def test_evaluate_answer_sends_rubric_and_reference() -> None:
    client = _MockClient(_evaluation())
    result = asyncio.run(
        evaluate_answer(
            question=_question(),
            user_answer="It moves partitions around.",
            level="intermediate",
            client=client,
            article_markdown=_ARTICLE,
        )
    )
    assert isinstance(result, AnswerEvaluation)
    sent = client.messages.calls[0]["messages"][-1]["content"]
    assert "what rebalancing is" in sent
    assert "model_answer: Rebalancing redistributes" in sent
    assert "Rebalancing details here." in sent      # anchored excerpt
    assert "Other content." not in sent             # sibling section excluded
    assert "candidate_answer: It moves partitions around." in sent
    assert "followup_answer:" not in sent


def test_combined_mode_forces_no_second_followup() -> None:
    # Model misbehaves and asks for another follow-up — worker overrides.
    client = _MockClient(
        _evaluation(needs_followup=True, followup_question="Again?")
    )
    result = asyncio.run(
        evaluate_answer(
            question=_question(),
            user_answer="First answer.",
            level="intermediate",
            client=client,
            followup_question="What triggers it?",
            followup_answer="A consumer joining or leaving.",
        )
    )
    assert result.needs_followup is False
    assert result.followup_question == ""
    sent = client.messages.calls[0]["messages"][-1]["content"]
    assert "followup_question: What triggers it?" in sent
    assert "followup_answer: A consumer joining or leaving." in sent


def test_section_excerpt_missing_anchor_falls_back_to_article() -> None:
    excerpt = _section_excerpt(_ARTICLE, "No such heading")
    assert "Rebalancing details here." in excerpt
    assert "Other content." in excerpt


def test_section_excerpt_slices_anchor_section() -> None:
    excerpt = _section_excerpt(_ARTICLE, "Consumer groups")
    assert excerpt.startswith("## Consumer groups")
    assert "Replication" not in excerpt
