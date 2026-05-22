import asyncio
import unittest.mock
from types import SimpleNamespace

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ArticleSection,
    Claim,
    EvidenceSpan,
    VisualIntent,
)
from pipeline.workers.drafting_worker import (
    draft_all_sections,
    draft_section,
    get_relevant_spans,
)


class MockMessages:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response_text = self.responses.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=response_text)]
        )


class MockAnthropicClient:
    def __init__(self, responses: list[str]) -> None:
        self.messages = MockMessages(responses)


def make_plan_with_spans() -> tuple[ArticlePlan, list[EvidenceSpan]]:
    spans = [
        EvidenceSpan(
            source_url="https://example.com/a",
            source_title="A",
            content="Alpha evidence.",
        ),
        EvidenceSpan(
            source_url="https://example.com/b",
            source_title="B",
            content="Beta evidence.",
        ),
        EvidenceSpan(
            source_url="https://example.com/c",
            source_title="C",
            content="Gamma evidence.",
        ),
    ]
    claims = [
        Claim(
            text="Alpha is supported.",
            source_ids=[str(spans[0].span_id)],
        ),
        Claim(
            text="Beta is supported.",
            source_ids=[str(spans[1].span_id)],
        ),
        Claim(
            text="Gamma is supported.",
            source_ids=[str(spans[2].span_id)],
        ),
    ]
    sections = [
        ArticleSection(
            title="First section",
            claim_ids=[str(claims[0].claim_id), str(claims[1].claim_id)],
        ),
        ArticleSection(
            title="Second section",
            claim_ids=[str(claims[2].claim_id)],
        ),
    ]
    plan = ArticlePlan(
        request=ArticleRequest(topic="Evidence-backed drafts"),
        sections=sections,
        claims=claims,
        visual_intents=[],
        evidence_span_ids=[str(span.span_id) for span in spans],
    )
    return plan, spans


def test_get_relevant_spans_returns_only_section_sources() -> None:
    plan, spans = make_plan_with_spans()

    relevant_spans = get_relevant_spans(plan.sections[0], plan, spans)

    assert relevant_spans == spans[:2]


def test_draft_section_extracts_citation_ids() -> None:
    plan, spans = make_plan_with_spans()
    client = MockAnthropicClient(
        ["A cache hit avoids a database round trip. [src:abc123]"]
    )

    draft = asyncio.run(
        draft_section(plan.sections[0], plan, spans[:2], client)
    )

    assert draft.title == "First section"
    assert draft.content == "A cache hit avoids a database round trip. [src:abc123]"
    assert draft.citation_ids == ["abc123"]
    # Drafter switched from opus-4-7 to sonnet-4-6 for the parallel-drafting
    # speedup. Quality is adequate for per-section writing; editor catches
    # anything weak.
    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"
    assert client.messages.calls[0]["max_tokens"] == 8192
    assert "claims_json" in client.messages.calls[0]["messages"][0]["content"]


def test_draft_all_sections_returns_package_with_all_sections() -> None:
    plan, spans = make_plan_with_spans()
    client = MockAnthropicClient(
        [
            "First section content. [src:one]",
            "Second section content. [src:two]",
        ]
    )

    draft_package = asyncio.run(draft_all_sections(plan, spans, client))

    assert draft_package.plan == plan
    assert len(draft_package.sections) == 2
    assert draft_package.sections[0].citation_ids == ["one"]
    assert draft_package.sections[1].citation_ids == ["two"]
    assert draft_package.raw_markdown == (
        "First section content. [src:one]\n\n"
        "Second section content. [src:two]"
    )


def test_draft_section_includes_user_topic_and_extra_context() -> None:
    """Drafter must see user_topic and user_extra_context so the section it
    writes serves the user's request, not just the planner's section notes."""
    plan, spans = make_plan_with_spans()
    plan = plan.model_copy(
        update={
            "request": ArticleRequest(
                topic="Evidence-backed drafts",
                extra_context="Cover citation conventions and trust scoring.",
            )
        }
    )
    client = MockAnthropicClient(["Section body. [src:one]"])

    asyncio.run(draft_section(plan.sections[0], plan, spans[:2], client))

    user_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "user_topic: Evidence-backed drafts" in user_prompt
    assert "user_extra_context: Cover citation conventions and trust scoring." in user_prompt


def test_draft_section_appends_diagram_placeholder_when_visual_intent_attached() -> None:
    """When a VisualIntent.section_title matches the section being drafted, the
    drafter must append a <!-- DIAGRAM:{intent_id} --> marker so the compiler
    can later substitute the rendered mermaid block."""
    plan, spans = make_plan_with_spans()
    intent = VisualIntent(
        description="Show evidence flow.",
        format="mermaid",
        rationale="Helps readers see the pipeline.",
        section_title="First section",
    )
    plan = plan.model_copy(update={"visual_intents": [intent]})

    client = MockAnthropicClient(["First section body. [src:one]"])
    draft = asyncio.run(draft_section(plan.sections[0], plan, spans[:2], client))

    assert f"<!-- DIAGRAM:{intent.intent_id} -->" in draft.content
    # Placeholder is appended after the LLM output, not inserted into it.
    assert draft.content.startswith("First section body. [src:one]")


def test_draft_section_skips_placeholder_when_intent_targets_different_section() -> None:
    """A VisualIntent attached to a different section must not leak a placeholder
    into the current section's content."""
    plan, spans = make_plan_with_spans()
    intent = VisualIntent(
        description="Show evidence flow.",
        format="mermaid",
        rationale="Helps readers.",
        section_title="Second section",  # NOT the section we'll draft.
    )
    plan = plan.model_copy(update={"visual_intents": [intent]})

    client = MockAnthropicClient(["First section body."])
    draft = asyncio.run(draft_section(plan.sections[0], plan, spans[:2], client))

    assert "<!-- DIAGRAM:" not in draft.content


def test_draft_section_retries_on_rate_limit_then_succeeds() -> None:
    """A 429 on the first attempt should be silently retried; the second
    successful response must be returned and only two API calls made."""
    plan, spans = make_plan_with_spans()
    call_count = 0

    class _Messages:
        calls: list = []

        async def create(self, **kwargs):
            nonlocal call_count
            call_count += 1
            self.calls.append(kwargs)
            if call_count == 1:
                raise Exception("rate_limit_error 429 exceeded")
            return SimpleNamespace(content=[SimpleNamespace(text="Retried OK. [src:ok]")])

    class _Client:
        messages = _Messages()

    async def _no_sleep(_: float) -> None:
        return None

    with unittest.mock.patch("asyncio.sleep", new=_no_sleep):
        draft = asyncio.run(
            draft_section(plan.sections[0], plan, spans[:2], _Client())
        )

    assert draft.content == "Retried OK. [src:ok]"
    assert call_count == 2


def test_draft_section_raises_after_all_retries_exhausted() -> None:
    """When every attempt returns a 429, the final exception must propagate
    after all max_retries attempts are made (no infinite loop)."""
    plan, spans = make_plan_with_spans()
    call_count = 0

    class _Messages:
        async def create(self, **kwargs):
            nonlocal call_count
            call_count += 1
            raise Exception("rate_limit_error 429 exceeded")

    class _Client:
        messages = _Messages()

    async def _no_sleep(_: float) -> None:
        return None

    with unittest.mock.patch("asyncio.sleep", new=_no_sleep):
        try:
            asyncio.run(
                draft_section(plan.sections[0], plan, spans[:2], _Client())
            )
            assert False, "Expected RateLimitError to propagate"
        except Exception as exc:
            assert "rate_limit" in str(exc).lower() or "429" in str(exc)

    # max_retries=5 → 5 total attempts before giving up
    assert call_count == 5


def test_draft_section_non_rate_limit_errors_propagate_immediately() -> None:
    """A non-429 error (e.g. auth failure) must not be retried — it should
    propagate on the very first attempt."""
    plan, spans = make_plan_with_spans()
    call_count = 0

    class _Messages:
        async def create(self, **kwargs):
            nonlocal call_count
            call_count += 1
            raise ValueError("invalid_api_key")

    class _Client:
        messages = _Messages()

    async def _no_sleep(_: float) -> None:
        return None

    with unittest.mock.patch("asyncio.sleep", new=_no_sleep):
        try:
            asyncio.run(
                draft_section(plan.sections[0], plan, spans[:2], _Client())
            )
            assert False, "Expected ValueError to propagate"
        except ValueError as exc:
            assert "invalid_api_key" in str(exc)

    assert call_count == 1  # no retry on non-rate-limit errors


def test_draft_section_skips_placeholder_when_intent_unattached() -> None:
    """An orphan VisualIntent (section_title=None) must produce no placeholder.
    Diagrams without a home should simply not appear in the article."""
    plan, spans = make_plan_with_spans()
    intent = VisualIntent(
        description="Show evidence flow.",
        format="mermaid",
        rationale="Helps readers.",
        section_title=None,
    )
    plan = plan.model_copy(update={"visual_intents": [intent]})

    client = MockAnthropicClient(["First section body."])
    draft = asyncio.run(draft_section(plan.sections[0], plan, spans[:2], client))

    assert "<!-- DIAGRAM:" not in draft.content
