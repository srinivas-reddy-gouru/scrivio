import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

import anthropic
import openai
from pydantic import BaseModel

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ClarificationState,
    DraftPackage,
    EditorReport,
    EvidenceSpan,
    PublishedArticle,
    RenderAsset,
    StoryBrief,
    VerificationReport,
)
from pipeline.workers.brief_worker import run_brief
from pipeline.workers.citation_utils import resolve_citations, scrub_em_dashes
from pipeline.workers.clarification_worker import run_clarification
from pipeline.workers.compiler_worker import compile_all_levels
from pipeline.workers.drafting_worker import draft_all_sections
from pipeline.workers.editor_worker import revise_draft, run_editor_review
from pipeline.workers.extraction_worker import process_search_result
from pipeline.workers.humanization_worker import humanize_article
from pipeline.workers.planning_worker import find_evidence_gaps, run_planner
from pipeline.workers.search_worker import multi_search
from pipeline.workers.verification_worker import (
    drop_unsupported_claims,
    run_verification_loop,
)
from render.mermaid_worker import process_visual_intent
from render.vhs_worker import process_vhs_intent


try:
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy
except ModuleNotFoundError:
    activity = SimpleNamespace(defn=lambda fn=None, **kwargs: fn)

    class _WorkflowFallback:
        def defn(self, cls=None, **kwargs):
            return cls

        def run(self, fn=None, **kwargs):
            return fn

        async def execute_activity(self, *args, **kwargs):
            raise RuntimeError("temporalio is not installed")

    workflow = _WorkflowFallback()

    @dataclass
    class RetryPolicy:
        maximum_attempts: int
        initial_interval: timedelta


ACTIVITY_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
)
DEFAULT_ACTIVITY_TIMEOUT = timedelta(minutes=10)
HEARTBEAT_TIMEOUT = timedelta(seconds=60)


MAX_FETCH_URLS = 10  # max URLs scraped per search pass

class SearchQueries(BaseModel):
    queries: list[str]


@activity.defn
async def clarification_activity(prompt: str) -> ClarificationState:
    return await run_clarification(prompt, openai.AsyncOpenAI())


@activity.defn
async def brief_activity(request: ArticleRequest) -> StoryBrief:
    return await run_brief(request, anthropic.AsyncAnthropic())


@activity.defn
async def search_activity(
    request: ArticleRequest, brief: StoryBrief | None = None
) -> list[EvidenceSpan]:
    if not request.web_search:
        return []

    client = openai.AsyncOpenAI()
    user_content = f"topic: {request.topic}"
    if brief:
        user_content += (
            f"\nthesis: {brief.thesis}"
            f"\nangle: {brief.angle}"
            f"\nkey_insight: {brief.key_insight}"
        )

    completion = await client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Generate exactly three targeted web search queries that will find "
                    "primary evidence for the given article thesis and angle. "
                    "Queries must be specific and focused — not generic topic overviews."
                ),
            },
            {"role": "user", "content": user_content},
        ],
        response_format=SearchQueries,
    )
    query_package = completion.choices[0].message.parsed
    if not isinstance(query_package, SearchQueries):
        query_package = SearchQueries.model_validate(query_package)

    search_results = await multi_search(query_package.queries[:3])
    span_groups = await asyncio.gather(
        *(process_search_result(result) for result in search_results[:MAX_FETCH_URLS])
    )
    return [span for group in span_groups for span in group]


@activity.defn
async def planning_activity(
    request: ArticleRequest,
    spans: list[EvidenceSpan],
    brief: StoryBrief | None = None,
) -> ArticlePlan:
    return await run_planner(request, spans, anthropic.AsyncAnthropic(), brief=brief)


@activity.defn
async def gap_fill_activity(
    plan: ArticlePlan, spans: list[EvidenceSpan]
) -> list[EvidenceSpan]:
    """Fetch evidence for any plan claims that have no matching spans."""
    gap_texts = find_evidence_gaps(plan, spans)
    if not gap_texts:
        return spans

    queries = gap_texts[:3]
    search_results = await multi_search(queries)
    if not search_results:
        return spans

    span_groups = await asyncio.gather(
        *(process_search_result(result) for result in search_results[:MAX_FETCH_URLS])
    )
    new_spans = [span for group in span_groups for span in group]
    return spans + new_spans


@activity.defn
async def drafting_activity(
    plan: ArticlePlan, spans: list[EvidenceSpan]
) -> DraftPackage:
    return await draft_all_sections(plan, spans, anthropic.AsyncAnthropic())


@activity.defn
async def visual_generation_activity(plan: ArticlePlan) -> list[RenderAsset]:
    client = openai.AsyncOpenAI()
    tasks = []

    for intent in plan.visual_intents:
        if intent.format == "vhs":
            tasks.append(process_vhs_intent(intent, client))
        elif intent.format == "mermaid":
            tasks.append(process_visual_intent(intent, client))
        else:
            tasks.append(_unsupported_visual_intent(intent))

    return list(await asyncio.gather(*tasks))


@activity.defn
async def verification_activity(
    plan: ArticlePlan, spans: list[EvidenceSpan]
) -> tuple[ArticlePlan, list[EvidenceSpan], list[VerificationReport]]:
    return await run_verification_loop(plan, spans, openai.AsyncOpenAI())


@activity.defn
async def editor_activity(
    plan: ArticlePlan, draft: DraftPackage
) -> EditorReport:
    return await run_editor_review(plan, draft, anthropic.AsyncAnthropic())


@activity.defn
async def revision_activity(
    plan: ArticlePlan,
    draft: DraftPackage,
    spans: list[EvidenceSpan],
    editor_report: EditorReport,
) -> DraftPackage:
    return await revise_draft(
        plan, draft, spans, editor_report, anthropic.AsyncAnthropic()
    )


@activity.defn
async def compilation_activity(
    draft: DraftPackage,
) -> dict[str, PublishedArticle]:
    compiled = await compile_all_levels(draft, anthropic.AsyncAnthropic())
    return {level: article for level, article in compiled.items()}


@activity.defn
async def humanization_activity(
    article: PublishedArticle, plan: ArticlePlan
) -> PublishedArticle:
    return await humanize_article(article, plan, anthropic.AsyncAnthropic())


@workflow.defn
class ArticleGenerationWorkflow:
    @workflow.run
    async def run(self, prompt: str) -> dict[str, PublishedArticle]:
        clarification = await _execute_activity(
            clarification_activity,
            prompt,
        )
        request = clarification.filled_request or ArticleRequest(topic=prompt)

        brief = await _execute_activity(brief_activity, request)

        spans = await _execute_activity(
            search_activity,
            request,
            brief,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )
        plan = await _execute_activity(planning_activity, request, spans, brief)
        spans = await _execute_activity(
            gap_fill_activity,
            plan,
            spans,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )

        verified_plan, verified_spans, reports = await _execute_activity(
            verification_activity, plan, spans
        )
        publishable_plan = drop_unsupported_claims(verified_plan)

        draft, assets = await asyncio.gather(
            _execute_activity(drafting_activity, publishable_plan, verified_spans),
            _execute_activity(
                visual_generation_activity,
                publishable_plan,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
            ),
        )

        editor_report = await _execute_activity(editor_activity, publishable_plan, draft)
        if not editor_report.approved and editor_report.revisions:
            draft = await _execute_activity(
                revision_activity,
                publishable_plan,
                draft,
                verified_spans,
                editor_report,
            )

        articles = await _execute_activity(compilation_activity, draft)

        humanized = await asyncio.gather(
            *(
                _execute_activity(humanization_activity, article, publishable_plan)
                for article in articles.values()
            )
        )
        articles = dict(zip(articles.keys(), humanized))

        for article in articles.values():
            article.markdown = resolve_citations(
                scrub_em_dashes(article.markdown), verified_spans
            )
            article.assets = assets
            article.verification_reports = reports

        return articles


async def _execute_activity(activity_fn, *args, heartbeat_timeout=None):
    return await workflow.execute_activity(
        activity_fn,
        args=args,
        start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
        heartbeat_timeout=heartbeat_timeout,
        retry_policy=ACTIVITY_RETRY_POLICY,
    )


async def _unsupported_visual_intent(intent) -> RenderAsset:
    return RenderAsset(intent=intent, spec="", output_path="", qa_passed=False)
