import asyncio

from pipeline.orchestrator import article_workflow
from pipeline.orchestrator.article_workflow import ArticleGenerationWorkflow
from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ArticleSection,
    Claim,
    ClarificationState,
    DraftPackage,
    DraftSection,
    EditorReport,
    EvidenceSpan,
    PublishedArticle,
    RenderAsset,
    StoryBrief,
    VerificationReport,
    VisualIntent,
)


def test_workflow_calls_activities_in_expected_order(monkeypatch) -> None:
    request = ArticleRequest(topic="Temporal pipelines")
    span = EvidenceSpan(
        source_url="https://example.com/temporal",
        content="Temporal runs durable workflows.",
    )
    claim = Claim(
        text="Temporal runs durable workflows.",
        source_ids=[str(span.span_id)],
    )
    section = ArticleSection(
        title="Durability",
        claim_ids=[str(claim.claim_id)],
    )
    visual_intent = VisualIntent(
        description="Show pipeline stages.",
        rationale="A flow diagram helps readers see the order.",
    )
    plan = ArticlePlan(
        request=request,
        sections=[section],
        claims=[claim],
        visual_intents=[visual_intent],
        evidence_span_ids=[str(span.span_id)],
    )
    draft = DraftPackage(
        plan=plan,
        sections=[
            DraftSection(
                title="Durability",
                content="Temporal runs durable workflows. [src:abc]",
                citation_ids=["abc"],
            )
        ],
        raw_markdown="Temporal runs durable workflows. [src:abc]",
    )
    asset = RenderAsset(
        intent=visual_intent,
        spec="flowchart LR\n  A --> B",
        output_path="/tmp/article_assets/diagram.svg",
        qa_passed=True,
    )
    report = VerificationReport(
        claim_id=str(claim.claim_id),
        support_status="supported",
    )
    compiled_article = PublishedArticle(
        request=request,
        title="Temporal pipelines",
        markdown="# Temporal pipelines\n",
    )
    brief = StoryBrief(
        thesis="Temporal workflows survive worker crashes by replaying execution history.",
        angle="deep-dive",
        reader_pain_point="Engineers lose in-flight task state when workers restart.",
        key_insight="Temporal's event log makes workflows deterministically replayable.",
        hook_seed="Your worker crashes mid-task. The queue is empty. The work is gone.",
        suggested_title="How Temporal Keeps Your Workflows Alive After a Crash",
    )
    calls = []

    async def fake_execute_activity(activity_fn, *args, heartbeat_timeout=None):
        calls.append(activity_fn.__name__)
        if activity_fn is article_workflow.clarification_activity:
            return ClarificationState(
                original_prompt="write about Temporal pipelines",
                filled_request=request,
                is_complete=True,
            )
        if activity_fn is article_workflow.brief_activity:
            return brief
        if activity_fn is article_workflow.search_activity:
            return [span]
        if activity_fn is article_workflow.planning_activity:
            return plan
        if activity_fn is article_workflow.gap_fill_activity:
            return [span]
        if activity_fn is article_workflow.verification_activity:
            return plan, [span], [report]
        if activity_fn is article_workflow.drafting_activity:
            return draft
        if activity_fn is article_workflow.visual_generation_activity:
            return [asset]
        if activity_fn is article_workflow.editor_activity:
            return EditorReport(approved=True, overall_assessment="Looks good.")
        if activity_fn is article_workflow.compilation_activity:
            return {"intermediate": compiled_article}
        if activity_fn is article_workflow.humanization_activity:
            return args[0]  # echo back the article unchanged
        raise AssertionError(f"Unexpected activity: {activity_fn}")

    monkeypatch.setattr(article_workflow, "_execute_activity", fake_execute_activity)

    result = asyncio.run(
        ArticleGenerationWorkflow().run("write about Temporal pipelines")
    )

    assert calls == [
        "clarification_activity",
        "brief_activity",
        "search_activity",
        "planning_activity",
        "gap_fill_activity",
        "verification_activity",
        "drafting_activity",
        "visual_generation_activity",
        "editor_activity",
        "compilation_activity",
        "humanization_activity",
    ]
    assert result == {"intermediate": compiled_article}
    assert result["intermediate"].assets == [asset]
    assert result["intermediate"].verification_reports == [report]
