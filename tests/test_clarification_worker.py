import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import ArticleRequest, ClarificationState
from pipeline.workers.clarification_worker import (
    interactive_clarify,
    run_clarification,
)


class MockCompletions:
    def __init__(self, states: list[ClarificationState]) -> None:
        self.states = states
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        state = self.states.pop(0)
        message = SimpleNamespace(parsed=state)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


class MockOpenAIClient:
    def __init__(self, states: list[ClarificationState]) -> None:
        self.completions = MockCompletions(states)
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )


def test_fully_specified_prompt_is_complete_with_no_questions() -> None:
    request = ArticleRequest(
        topic="database indexing",
        explanation_level="advanced",
        audience_role="database engineer",
        web_search=True,
        max_source_age_days=30,
        include_gifs=False,
        include_diagrams=True,
    )
    client = MockOpenAIClient(
        [
            ClarificationState(
                original_prompt="Write an advanced article about database indexing.",
                filled_request=request,
                questions_asked=[],
                is_complete=True,
            )
        ]
    )

    state = asyncio.run(
        run_clarification(
            "Write an advanced article about database indexing.", client
        )
    )

    assert state.is_complete is True
    assert state.questions_asked == []
    assert state.filled_request == request
    assert client.completions.calls[0]["model"] == "gpt-4o-mini"
    assert client.completions.calls[0]["response_format"] is ClarificationState


def test_vague_prompt_returns_one_question() -> None:
    client = MockOpenAIClient(
        [
            ClarificationState(
                original_prompt="write about databases",
                filled_request=None,
                questions_asked=["What kind of database topic should the article focus on?"],
                is_complete=False,
            )
        ]
    )

    state = asyncio.run(run_clarification("write about databases", client))

    assert state.is_complete is False
    assert state.filled_request is None
    assert state.questions_asked == [
        "What kind of database topic should the article focus on?"
    ]


def test_interactive_loop_terminates_after_three_iterations(monkeypatch) -> None:
    client = MockOpenAIClient(
        [
            ClarificationState(
                original_prompt="write about databases",
                questions_asked=["Which database angle should it cover?"],
                is_complete=False,
            ),
            ClarificationState(
                original_prompt="write about databases",
                questions_asked=["Which database angle should it cover?"],
                is_complete=False,
            ),
            ClarificationState(
                original_prompt="write about databases",
                questions_asked=["Which database angle should it cover?"],
                is_complete=False,
            ),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda: "transaction isolation")

    request = asyncio.run(interactive_clarify("write about databases", client))

    assert request == ArticleRequest(topic="write about databases")
    assert len(client.completions.calls) == 3
