import asyncio
from types import SimpleNamespace

import pytest

from pipeline.schemas.models import (
    ArticleRequest,
    EvidenceSpan,
)
from pipeline.workers.planning_worker import (
    PlannerError,
    load_evidence_context,
    run_planner,
)


class MockMessages:
    def __init__(self, tool_input: dict) -> None:
        self.tool_input = tool_input
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="submit_article_plan",
                    input=self.tool_input,
                )
            ]
        )


class MockAnthropicClient:
    def __init__(self, tool_input: dict) -> None:
        self.messages = MockMessages(tool_input)


def make_span(index: int, trust_score: float = 0.8) -> EvidenceSpan:
    return EvidenceSpan(
        source_url=f"https://example.com/source-{index}",
        source_title=f"Source {index}",
        content=f"Evidence content for span {index}.",
        trust_score=trust_score,
    )


def planner_output_for(span: EvidenceSpan) -> dict:
    """Build a _PlannerOutput-shaped dict for a single span."""
    return {
        "sections": [
            {
                "title": "Durable execution",
                "claim_ids": ["c1"],
                "notes": "",
                "narrative_note": "Open with the hook seed about retry guarantees.",
            }
        ],
        "claims": [
            {
                "id": "c1",
                "text": "Temporal workflows can preserve execution state across failures.",
                "source_ids": [str(span.span_id)],
                "freshness_sensitive": False,
            }
        ],
        "visual_intents": [
            {
                "description": "Show workflow state surviving a retry.",
                "format": "mermaid",
                "rationale": "The relationship between retry and state is easier to see visually.",
                "section_title": "Durable execution",
            }
        ],
        "evidence_span_ids": [str(span.span_id)],
    }


def test_load_evidence_context_includes_span_ids_and_truncates() -> None:
    spans = [make_span(index, trust_score=index / 10) for index in range(10)]

    full_context = load_evidence_context(spans, max_chars=10000)
    truncated_context = load_evidence_context(spans, max_chars=120)

    for span in spans:
        assert str(span.span_id) in full_context

    assert len(truncated_context) <= 120
    assert str(spans[-1].span_id) in truncated_context


def test_run_planner_parses_tool_use_into_article_plan() -> None:
    request = ArticleRequest(topic="Temporal workflow retries")
    span = make_span(1)
    client = MockAnthropicClient(planner_output_for(span))

    parsed = asyncio.run(run_planner(request, [span], client))

    # Structure was assembled correctly from the planner output.
    assert parsed.request == request
    assert len(parsed.claims) == 1
    assert parsed.claims[0].source_ids == [str(span.span_id)]
    assert len(parsed.sections) == 1
    # Sections' claim_ids were rewritten from "c1" to the real Claim UUID.
    assert parsed.sections[0].claim_ids == [str(parsed.claims[0].claim_id)]
    assert parsed.evidence_span_ids == [str(span.span_id)]
    assert len(parsed.visual_intents) == 1

    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 8192
    assert call["tool_choice"]["name"] == "submit_article_plan"
    assert "article_request" in call["messages"][0]["content"]
    assert str(span.span_id) in call["messages"][0]["content"]


def test_run_planner_raises_when_claim_has_empty_source_ids() -> None:
    request = ArticleRequest(topic="Temporal workflow retries")
    span = make_span(1)
    output = planner_output_for(span)
    output["claims"][0]["source_ids"] = []
    client = MockAnthropicClient(output)

    with pytest.raises(PlannerError, match="without source_ids"):
        asyncio.run(run_planner(request, [span], client))


def test_run_planner_drops_unknown_claim_refs_from_sections() -> None:
    """If the model references an unknown claim id, the section drops it
    rather than crashing — verification will then flag the orphan."""
    request = ArticleRequest(topic="Temporal workflow retries")
    span = make_span(1)
    output = planner_output_for(span)
    # Add a phantom reference that doesn't match any real claim.
    output["sections"][0]["claim_ids"] = ["c1", "c99"]
    client = MockAnthropicClient(output)

    parsed = asyncio.run(run_planner(request, [span], client))

    # Only the real claim survives; "c99" is dropped silently.
    assert parsed.sections[0].claim_ids == [str(parsed.claims[0].claim_id)]


# ── find_evidence_gaps: low-trust sole-source guard ─────────────────────

def _plan_with_claims(*claims_spec) -> "SimpleNamespace":
    """Minimal plan stand-in: find_evidence_gaps only reads plan.claims,
    and each claim only needs .text and .source_ids."""
    return SimpleNamespace(claims=[
        SimpleNamespace(text=text, source_ids=source_ids)
        for text, source_ids in claims_spec
    ])


def test_find_evidence_gaps_flags_low_trust_only_claims() -> None:
    """A claim grounded ONLY in penalized-tier sources (trust <= 0.5:
    social/UGC at 0.5, forums at 0.45) is a gap — re-search rather than
    let a LinkedIn post carry a section. Regression: a July 2026 article's
    LazyInitializationException section was sourced 4x from a LinkedIn
    Pulse post after Jina 401s thinned the evidence pool."""
    from pipeline.workers.planning_worker import find_evidence_gaps

    linkedin = make_span(1, trust_score=0.5)
    forum = make_span(2, trust_score=0.45)
    official = make_span(3, trust_score=1.0)

    plan = _plan_with_claims(
        ("ugc-only claim", [str(linkedin.span_id), str(forum.span_id)]),
        ("well-sourced claim", [str(official.span_id), str(linkedin.span_id)]),
    )
    gaps = find_evidence_gaps(plan, [linkedin, forum, official])

    assert gaps == ["ugc-only claim"]


def test_find_evidence_gaps_orders_ungrounded_before_low_trust() -> None:
    """The caller caps gap queries at 3, so fully ungrounded claims (which
    will fail verification outright) must win the budget over claims that
    are merely weakly sourced."""
    from pipeline.workers.planning_worker import find_evidence_gaps

    ugc = make_span(1, trust_score=0.5)
    plan = _plan_with_claims(
        ("low-trust claim", [str(ugc.span_id)]),
        ("ungrounded claim", ["hallucinated-span-id"]),
    )
    gaps = find_evidence_gaps(plan, [ugc])

    assert gaps == ["ungrounded claim", "low-trust claim"]


def test_find_evidence_gaps_accepts_unknown_blog_as_sole_source() -> None:
    """Unknown HTTPS sites (0.6) and blog platforms (0.65) sit above the
    ceiling — they remain acceptable sole sources, so the guard must not
    turn every blog-cited claim into a search query."""
    from pipeline.workers.planning_worker import find_evidence_gaps

    blog = make_span(1, trust_score=0.6)
    plan = _plan_with_claims(("blog-sourced claim", [str(blog.span_id)]))

    assert find_evidence_gaps(plan, [blog]) == []
