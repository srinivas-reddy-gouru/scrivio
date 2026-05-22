from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ArticleSection,
    Claim,
    ClarificationState,
    DraftPackage,
    DraftSection,
    EvidenceSpan,
    PublishedArticle,
    RenderAsset,
    StoryBrief,
    VerificationReport,
    VisualIntent,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


def assert_round_trip(instance: ModelT) -> None:
    dumped = instance.model_dump(mode="json")
    restored = type(instance).model_validate(dumped)

    assert restored.model_dump(mode="json") == dumped


def sample_article_request() -> ArticleRequest:
    return ArticleRequest(
        topic="Temporal article pipelines",
        explanation_level="advanced",
        audience_role="backend engineer",
        web_search=False,
        max_source_age_days=90,
        include_gifs=True,
        include_diagrams=True,
        extra_context="Focus on reliability.",
    )


def sample_evidence_span() -> EvidenceSpan:
    return EvidenceSpan(
        source_url="https://example.com/reliable-source",
        source_title="Reliable Source",
        content="Temporal workflows are durable execution systems.",
        published_at=datetime(2025, 1, 2, 3, 4, 5),
        trust_score=0.95,
        was_filtered=True,
    )


def sample_claim() -> Claim:
    evidence = sample_evidence_span()
    return Claim(
        text="Temporal can retry failed activities.",
        source_ids=[str(evidence.span_id)],
        support_status="supported",
        freshness_sensitive=True,
        corrective_attempts=1,
    )


def sample_visual_intent() -> VisualIntent:
    return VisualIntent(
        description="Show workflow and activity retry flow.",
        format="mermaid",
        rationale="A sequence diagram makes retries easy to scan.",
    )


def sample_article_section() -> ArticleSection:
    claim = sample_claim()
    return ArticleSection(
        title="Retries",
        claim_ids=[str(claim.claim_id)],
        notes="Mention idempotency.",
    )


def sample_article_plan() -> ArticlePlan:
    evidence = sample_evidence_span()
    claim = Claim(
        text="Temporal workflows preserve state across worker restarts.",
        source_ids=[str(evidence.span_id)],
    )
    section = ArticleSection(title="Durability", claim_ids=[str(claim.claim_id)])
    visual = sample_visual_intent()

    return ArticlePlan(
        request=sample_article_request(),
        sections=[section],
        claims=[claim],
        visual_intents=[visual],
        evidence_span_ids=[str(evidence.span_id)],
    )


def sample_draft_section() -> DraftSection:
    claim = sample_claim()
    return DraftSection(
        title="Retries",
        content="Activities can be retried according to configured policies.",
        citation_ids=[str(claim.claim_id)],
    )


def sample_draft_package() -> DraftPackage:
    section = sample_draft_section()
    return DraftPackage(
        plan=sample_article_plan(),
        sections=[section],
        raw_markdown=f"## {section.title}\n\n{section.content}",
    )


def sample_render_asset() -> RenderAsset:
    return RenderAsset(
        intent=sample_visual_intent(),
        spec="sequenceDiagram\n  participant W as Workflow\n  participant A as Activity",
        output_path="render/workflow-retries.svg",
        qa_passed=True,
    )


def sample_verification_report() -> VerificationReport:
    claim = sample_claim()
    return VerificationReport(
        claim_id=str(claim.claim_id),
        support_status="weak",
        verifier_note="Source partially supports the claim.",
    )


def test_story_brief_round_trips() -> None:
    assert_round_trip(
        StoryBrief(
            thesis="PostgreSQL's planner ignores indexes when statistics are stale.",
            angle="contrarian",
            reader_pain_point="Engineers add indexes that the planner never uses.",
            key_insight="Running ANALYZE after bulk loads restores planner accuracy.",
            hook_seed="EXPLAIN shows a sequential scan on a 10M-row table with an index on the query column.",
            suggested_title="Why PostgreSQL Ignores Your Index (and What To Do About It)",
        )
    )


def test_article_request_round_trips() -> None:
    assert_round_trip(sample_article_request())


def test_clarification_state_round_trips() -> None:
    assert_round_trip(
        ClarificationState(
            original_prompt="Explain Temporal retries.",
            filled_request=sample_article_request(),
            questions_asked=["Who is the audience?"],
            is_complete=True,
        )
    )


def test_evidence_span_round_trips() -> None:
    assert_round_trip(sample_evidence_span())


def test_claim_round_trips() -> None:
    assert_round_trip(sample_claim())


def test_visual_intent_round_trips() -> None:
    assert_round_trip(sample_visual_intent())


def test_article_section_round_trips() -> None:
    assert_round_trip(sample_article_section())


def test_article_plan_round_trips() -> None:
    assert_round_trip(sample_article_plan())


def test_draft_section_round_trips() -> None:
    assert_round_trip(sample_draft_section())


def test_draft_package_round_trips() -> None:
    assert_round_trip(sample_draft_package())


def test_render_asset_round_trips() -> None:
    assert_round_trip(sample_render_asset())


def test_verification_report_round_trips() -> None:
    assert_round_trip(sample_verification_report())


def test_published_article_round_trips() -> None:
    assert_round_trip(
        PublishedArticle(
            request=sample_article_request(),
            title="Temporal Article Pipelines",
            markdown="# Temporal Article Pipelines\n",
            assets=[sample_render_asset()],
            verification_reports=[sample_verification_report()],
            created_at=datetime(2025, 2, 3, 4, 5, 6),
        )
    )
