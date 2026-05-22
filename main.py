import argparse
import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import anthropic
import openai
from pydantic import BaseModel

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ArticleSection,
    Claim,
    DraftPackage,
    EditorReport,
    EvidenceSpan,
    ExplanationLevel,
    ProgressEvent,
    PublishedArticle,
    RenderAsset,
    StoryBrief,
    VerificationReport,
    VisualIntent,
)
from pipeline.workers.brief_worker import run_brief
from pipeline.workers.clarification_worker import interactive_clarify
from pipeline.workers.compiler_worker import compile_all_levels
from pipeline.workers.citation_utils import resolve_citations, scrub_em_dashes
from pipeline.workers.drafting_worker import draft_all_sections
from pipeline.workers.editor_worker import revise_draft, run_editor_review
from pipeline.cache import StageCache
from pipeline.workers.extraction_worker import process_search_result, score_url
from pipeline.workers.critic_worker import critique_article
from pipeline.workers.humanization_worker import humanize_article, polish_draft_to_article
from pipeline.workers.planning_worker import find_evidence_gaps, run_planner
from pipeline.workers.relevance_worker import (
    amend_request_with_missing_aspects,
    check_relevance,
)
from pipeline.providers.openai_adapter import OpenAIAnthropicAdapter
from pipeline.workers.search_worker import multi_search
from pipeline.workers.verification_worker import (
    drop_unsupported_claims,
    run_verification_loop,
)
from render.mermaid_worker import process_visual_intent
from render.vhs_worker import process_vhs_intent


MAX_FETCH_URLS = 10
# Fallback search thresholds: if the initial fetch produces fewer than
# MIN_USEFUL_SPANS total chunks OR fewer than MIN_USEFUL_SOURCES URLs that
# yielded content, a second search pass runs with broader queries to avoid
# producing an under-sourced article silently.
MIN_USEFUL_SPANS = 15
MIN_USEFUL_SOURCES = 3

class SearchQueries(BaseModel):
    queries: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate sourced technical articles.")
    parser.add_argument("--topic", default="")
    parser.add_argument(
        "--level",
        choices=["basic", "intermediate", "advanced"],
        default="intermediate",
    )
    parser.add_argument("--audience", default="software engineer")
    parser.add_argument("--no-web", action="store_true", help="Disable web search")
    parser.add_argument("--no-diagrams", action="store_true", help="Skip diagrams")
    parser.add_argument("--with-gifs", action="store_true", help="Enable GIF assets")
    parser.add_argument("--extra-context", default="", help="Extra guidance for the article")
    parser.add_argument("--out", default="./output", help="Output directory")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Ask clarifying questions before generating (requires OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--mode",
        choices=["direct", "temporal"],
        default="direct",
        help="Run locally or through Temporal",
    )
    return parser.parse_args()


ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


async def _emit(
    callback: ProgressCallback | None,
    type_: str,
    stage: str,
    message: str = "",
    **data,
) -> None:
    if callback is None:
        return
    await callback(ProgressEvent(type=type_, stage=stage, message=message, data=data))


async def generate_article(
    request: ArticleRequest,
    *,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, PublishedArticle]:
    """Run the full article pipeline. Pure function — no CLI, no stdin.

    Emits ProgressEvent objects via progress_callback at each stage boundary
    so callers (CLI, HTTP API) can stream progress without re-deriving it.

    Expensive stages (brief, search, planning, verification, drafting) are
    transparently cached by input hash. Reruns with the same inputs reuse
    cached results — invaluable when a late-stage bug forces a retry.
    """
    openai_client = _openai_client(request)
    anthropic_client = _anthropic_client(request)
    cache = StageCache()

    # ─── brief ────────────────────────────────────────────────────────
    await _emit(progress_callback, "stage_started", "brief", "Setting editorial angle")
    brief_key = (request.topic, request.explanation_level, request.audience_role, request.extra_context or "")
    cached_brief = cache.get("brief", *brief_key)
    if cached_brief is not None:
        brief = StoryBrief.model_validate(cached_brief)
    else:
        brief = await _generate_brief(request, anthropic_client)
        if brief is not None:
            cache.set("brief", brief, *brief_key)
    await _emit(
        progress_callback, "stage_completed", "brief",
        cached=cached_brief is not None,
        # Surface the full brief so the debug pane shows the user the
        # angle, thesis, hook, and title the model picked. This is the
        # single most useful piece of debug info — if the brief drifted,
        # this is where you see it first.
        brief=brief.model_dump(mode="json") if brief is not None else None,
    )

    # ─── relevance check (defense in depth) ───────────────────────────
    # Catches brief drift before the expensive search/plan/draft stages.
    # If misaligned, regenerate the brief ONCE with missing aspects injected
    # into extra_context. No second check — capped at 1 retry to bound cost.
    if brief is not None:
        await _emit(
            progress_callback, "stage_started", "relevance_check",
            "Checking brief against user request",
        )
        relevance = await check_relevance(request, brief, anthropic_client)
        if not relevance.aligned and relevance.missing_aspects:
            logging.info(
                "Relevance check failed: missing %s. Regenerating brief.",
                relevance.missing_aspects,
            )
            # Amend the request so every downstream stage (planner, drafter,
            # editor) sees the missing aspects, not just the brief retry.
            request = amend_request_with_missing_aspects(
                request, relevance.missing_aspects
            )
            # Re-key the brief cache with the new extra_context. The retry
            # may still cache-hit if this exact amendment has been tried
            # before; otherwise it generates fresh.
            brief_key = (
                request.topic, request.explanation_level,
                request.audience_role, request.extra_context or "",
            )
            cached_retry = cache.get("brief", *brief_key)
            if cached_retry is not None:
                brief = StoryBrief.model_validate(cached_retry)
            else:
                brief = await _generate_brief(request, anthropic_client)
                if brief is not None:
                    cache.set("brief", brief, *brief_key)
        await _emit(
            progress_callback, "stage_completed", "relevance_check",
            aligned=relevance.aligned,
            missing_aspects_count=len(relevance.missing_aspects),
            brief_regenerated=not relevance.aligned and bool(relevance.missing_aspects),
            # Full verdict for the debug pane — including the verifier's
            # reasoning and what aspects it thought were missing.
            missing_aspects=relevance.missing_aspects,
            suggested_thesis_adjustment=relevance.suggested_thesis_adjustment,
            reasoning=relevance.reasoning,
        )

    # ─── search ───────────────────────────────────────────────────────
    await _emit(progress_callback, "stage_started", "search", "Gathering evidence")
    search_key = (
        request.topic,
        brief.thesis if brief else "",
        brief.angle if brief else "",
        request.web_search,
    )
    cached_spans = cache.get("search", *search_key)
    search_debug: dict = {}
    if cached_spans is not None:
        spans = [EvidenceSpan.model_validate(s) for s in cached_spans]
        # For cached runs, reconstruct a minimal URL list from the spans
        # themselves so the debug pane still shows the user where the
        # evidence came from.
        seen_urls = {}
        for s in spans:
            seen_urls.setdefault(s.source_url, 0)
            seen_urls[s.source_url] += 1
        search_debug["urls"] = [
            {"url": url, "spans_yielded": n, "status": "cached"}
            for url, n in list(seen_urls.items())[:20]
        ]
    else:
        spans = await _collect_evidence_spans(
            request, brief, openai_client, debug_info=search_debug,
        )
        cache.set("search", spans, *search_key)
    await _emit(
        progress_callback, "stage_completed", "search",
        spans_count=len(spans), cached=cached_spans is not None,
        **search_debug,
    )

    # ─── planning ─────────────────────────────────────────────────────
    await _emit(progress_callback, "stage_started", "planning", "Outlining sections")
    plan_key = (request, brief, sorted(str(s.span_id) for s in spans))
    cached_plan = cache.get("planning", *plan_key)
    if cached_plan is not None:
        plan = ArticlePlan.model_validate(cached_plan)
    else:
        plan = await run_planner(request, spans, anthropic_client, brief=brief)
        cache.set("planning", plan, *plan_key)
    await _emit(
        progress_callback, "stage_completed", "planning",
        sections=len(plan.sections), cached=cached_plan is not None,
        # Show the user the actual section breakdown the planner picked,
        # plus the claim count and how many diagrams it requested. If
        # the article comes out wrong-shaped, this is where it started.
        section_titles=[s.title for s in plan.sections],
        claims_count=len(plan.claims),
        visual_intents_count=len(plan.visual_intents),
        visual_intents=[
            {
                "description": vi.description[:160],
                "format": vi.format,
                "section_title": vi.section_title or "(unbound)",
            }
            for vi in plan.visual_intents
        ],
    )

    # ─── gap fill (cached together with the spans it produces) ────────
    await _emit(progress_callback, "stage_started", "gap_fill", "Filling evidence gaps")
    gap_key = (plan_key, sorted(c.text for c in plan.claims))
    cached_gap_spans = cache.get("gap_fill", *gap_key)
    if cached_gap_spans is not None:
        spans = [EvidenceSpan.model_validate(s) for s in cached_gap_spans]
    else:
        spans = await _fill_evidence_gaps(plan, spans, openai_client)
        cache.set("gap_fill", spans, *gap_key)
    await _emit(
        progress_callback, "stage_completed", "gap_fill",
        spans_count=len(spans), cached=cached_gap_spans is not None,
        # If no new spans were added, gap fill found everything was
        # already grounded. The user can tell at a glance.
        new_spans_added=len(spans) - (len(cached_gap_spans) if cached_gap_spans else 0),
    )

    if not request.include_diagrams and not request.include_gifs:
        plan.visual_intents = []
    elif not request.include_gifs:
        plan.visual_intents = [
            intent for intent in plan.visual_intents if intent.format != "vhs"
        ]

    # ─── verification ─────────────────────────────────────────────────
    await _emit(progress_callback, "stage_started", "verification", "Fact-checking claims")
    verify_key = (plan, sorted(str(s.span_id) for s in spans))
    cached_verify = cache.get("verification", *verify_key)
    if cached_verify is not None:
        verified_plan = ArticlePlan.model_validate(cached_verify["plan"])
        verified_spans = [EvidenceSpan.model_validate(s) for s in cached_verify["spans"]]
        reports = [VerificationReport.model_validate(r) for r in cached_verify["reports"]]
    else:
        verified_plan, verified_spans, reports = await run_verification_loop(
            plan, spans, openai_client
        )
        cache.set(
            "verification",
            {"plan": verified_plan, "spans": verified_spans, "reports": reports},
            *verify_key,
        )
    publishable_plan = drop_unsupported_claims(verified_plan)
    await _emit(
        progress_callback, "stage_completed", "verification",
        supported=sum(r.support_status == "supported" for r in reports),
        total=len(reports),
        cached=cached_verify is not None,
        # Full breakdown so the user can see exactly which claims passed
        # which axes of verification. Two-axis verdict from Sprint 5.
        weak=sum(r.support_status == "weak" for r in reports),
        unsupported=sum(r.support_status == "unsupported" for r in reports),
        off_topic=sum(r.relevance_status == "off_topic" for r in reports),
        tangential=sum(r.relevance_status == "tangential" for r in reports),
        claims_dropped=len(verified_plan.claims) - len(publishable_plan.claims),
        # First few verifier notes for debugging — full ones are in meta.json.
        sample_verdicts=[
            {
                "support": r.support_status,
                "relevance": r.relevance_status,
                "note": r.verifier_note[:140],
            }
            for r in reports[:6]
        ],
    )

    # ─── drafting + visuals ───────────────────────────────────────────
    await _emit(progress_callback, "stage_started", "drafting", "Writing sections")
    draft_key = (publishable_plan, sorted(str(s.span_id) for s in verified_spans))
    cached_draft = cache.get("drafting", *draft_key)
    if cached_draft is not None:
        draft = DraftPackage.model_validate(cached_draft)
        assets = await _generate_assets(publishable_plan, anthropic_client)
    else:
        draft, assets = await asyncio.gather(
            draft_all_sections(publishable_plan, verified_spans, anthropic_client),
            _generate_assets(publishable_plan, anthropic_client),
        )
        cache.set("drafting", draft, *draft_key)
    await _emit(
        progress_callback, "stage_completed", "drafting",
        sections=len(draft.sections), cached=cached_draft is not None,
        # Per-section word counts give the user a quick sanity check
        # on whether any section came back suspiciously short or long.
        section_word_counts=[
            {"title": s.title, "words": len(s.content.split())}
            for s in draft.sections
        ],
        total_words=sum(len(s.content.split()) for s in draft.sections),
        assets_rendered=sum(1 for a in assets if a.qa_passed),
        assets_failed=sum(1 for a in assets if not a.qa_passed),
    )

    await _emit(progress_callback, "stage_started", "editor", "Editorial review")
    # Cache key: the draft being reviewed + the plan that drives the review.
    # If a prior run crashed after the editor succeeded (e.g. rate-limit in
    # polish), a retry must not call the editor LLM again — the result is
    # deterministic for the same inputs.
    editor_key = (publishable_plan, draft)
    cached_editor = cache.get("editor", *editor_key)
    if cached_editor is not None:
        editor_report = EditorReport.model_validate(cached_editor["report"])
        draft = DraftPackage.model_validate(cached_editor["final_draft"])
    else:
        editor_report = await run_editor_review(publishable_plan, draft, anthropic_client)
        # Trigger revise_draft when there are revision corrections (editor
        # disapproved one or more sections) OR when the editor approved the
        # draft but emitted structural hints (comparison tables, labelled
        # lists). Without this second condition, structural hints are silently
        # dropped whenever the editor returns approved=True — the most common
        # path — and the comparison table / summary list never appears.
        needs_revision = bool(
            (not editor_report.approved and editor_report.revisions)
            or editor_report.structural_hints
        )
        if needs_revision:
            logging.info(
                "Editor: %d revision(s), %d structural hint(s) — re-drafting affected sections.",
                len(editor_report.revisions),
                len(editor_report.structural_hints),
            )
            draft = await revise_draft(
                publishable_plan, draft, verified_spans, editor_report, anthropic_client
            )
        # Cache both the report AND the post-revision draft together so a
        # retry at any later stage can skip both the review and the revision.
        cache.set(
            "editor",
            {"report": editor_report, "final_draft": draft},
            *editor_key,
        )
    await _emit(
        progress_callback,
        "stage_completed",
        "editor",
        approved=editor_report.approved,
        revisions=len(editor_report.revisions),
        overall_assessment=editor_report.overall_assessment,
        cached=cached_editor is not None,
        revision_targets=[
            {
                "section_title": r.section_title,
                "issues": r.issues,
                "instruction": r.instruction,
            }
            for r in editor_report.revisions
        ],
        structural_hints=[
            {
                "section_title": h.section_title,
                "hint": h.hint,
            }
            for h in editor_report.structural_hints
        ],
    )

    # ─── polish (combined compile + humanize) ─────────────────────────
    # Single LLM pass replaces the old compile → humanize sequence. The
    # humanizer prompt was extended to also adapt vocabulary/depth to the
    # requested explanation level, which the compiler used to do. One
    # full-article generation instead of two — halves the post-draft
    # latency and cost.
    await _emit(
        progress_callback, "stage_started", "polish",
        f"Adapting for {request.explanation_level} level and polishing voice",
    )
    # Cache key: the post-editor draft + plan. If the critic later forces a
    # refinement pass, that second polish is intentionally NOT cached (its
    # inputs differ — critic_feedback is added), so only the first pass is.
    polish_key = (publishable_plan, draft)
    cached_polish = cache.get("polish", *polish_key)
    if cached_polish is not None:
        polished_article = PublishedArticle.model_validate(cached_polish)
    else:
        polished_article = await polish_draft_to_article(
            draft, publishable_plan, anthropic_client, assets=assets,
        )
        cache.set("polish", polished_article, *polish_key)
    await _emit(
        progress_callback, "stage_completed", "polish",
        levels=[request.explanation_level],
        output_word_count=len(polished_article.markdown.split()),
        cached=cached_polish is not None,
    )

    # ─── critic (the polish-layer quality gate) ───────────────────────
    # Reads the polished article as a published unit and decides if any
    # issues warrant one more polish pass. The verifier checks claim-
    # level facts; the editor checks the draft before polish. The critic
    # is the only agent that sees the FINAL article and reviews it the
    # way a senior editor would.
    await _emit(
        progress_callback, "stage_started", "critic",
        "Reviewing final article",
    )
    verdict = await critique_article(
        polished_article.markdown, publishable_plan, anthropic_client,
    )
    blocking_count = sum(1 for i in verdict.issues if i.severity == "blocking")
    if verdict.has_blocking_issues():
        logging.info(
            "Critic flagged %d blocking issue(s); running one polish refinement.",
            blocking_count,
        )
        # Refinement pass: start from the polished markdown, feed the
        # critic's issues in. Cap at 1 retry — we don't re-critique;
        # the assumption is the humanizer addresses what it was told to
        # and additional passes hit diminishing returns.
        polished_article = await polish_draft_to_article(
            draft, publishable_plan, anthropic_client, assets=assets,
            critic_feedback=verdict.issues,
            prior_markdown=polished_article.markdown,
        )
    articles = {request.explanation_level: polished_article}
    await _emit(
        progress_callback, "stage_completed", "critic",
        approved=verdict.approved,
        blocking=blocking_count,
        moderate=sum(1 for i in verdict.issues if i.severity == "moderate"),
        minor=sum(1 for i in verdict.issues if i.severity == "minor"),
        overall_assessment=verdict.overall_assessment,
        refined=verdict.has_blocking_issues(),
        issues=[i.model_dump(mode="json") for i in verdict.issues],
    )

    for article in articles.values():
        article.markdown = resolve_citations(
            scrub_em_dashes(article.markdown), verified_spans
        )
        article.assets = assets
        article.verification_reports = reports

    return {level: article for level, article in articles.items()}


async def run_direct(args) -> dict[str, PublishedArticle]:
    """CLI entry point. Builds an ArticleRequest from argparse and delegates."""
    if args.interactive:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("--interactive requires OPENAI_API_KEY to be set")
        seed = args.topic or input("What would you like to write about? ").strip()
        request = await interactive_clarify(seed, openai.AsyncOpenAI())
    else:
        if not args.topic:
            raise RuntimeError("--topic is required unless --interactive is set")
        request = ArticleRequest(
            topic=args.topic,
            explanation_level=args.level,
            audience_role=args.audience,
            web_search=not args.no_web,
            include_gifs=args.with_gifs,
            include_diagrams=not args.no_diagrams,
            extra_context=args.extra_context,
        )
    return await generate_article(request)


async def run_temporal(args) -> dict[str, PublishedArticle]:
    try:
        from temporalio.client import Client
    except ModuleNotFoundError as exc:
        raise RuntimeError("temporalio is required for --mode temporal") from exc

    from pipeline.orchestrator.article_workflow import ArticleGenerationWorkflow
    from pipeline.orchestrator.run_worker import TASK_QUEUE

    client = await Client.connect(os.environ["TEMPORAL_HOST"])
    return await client.execute_workflow(
        ArticleGenerationWorkflow.run,
        args.topic,
        id=f"article-{_slug(args.topic)}",
        task_queue=TASK_QUEUE,
    )


def save_articles(
    articles: dict[str, PublishedArticle], output_dir: str
) -> dict[str, Path]:
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    paths = {}

    for level, article in articles.items():
        file_path = out_path / f"{level}.md"
        file_path.write_text(article.markdown, encoding="utf-8")
        paths[level] = file_path

    return paths


def print_summary(articles: dict[str, PublishedArticle], paths: dict[str, Path]) -> None:
    article = next(iter(articles.values()))
    claims_total = len(article.verification_reports)
    verified_count = sum(
        report.support_status == "supported" for report in article.verification_reports
    )
    asset_count = len(article.assets)

    print(f"Title: {article.title}")
    print(
        f"Summary: claims total={claims_total}, "
        f"verified={verified_count}, assets={asset_count}"
    )
    for level, path in paths.items():
        print(f"{level}: {path}")


async def _generate_brief(
    request: ArticleRequest, anthropic_client
) -> StoryBrief | None:
    if isinstance(anthropic_client, MockAnthropicClient):
        return StoryBrief(
            thesis=f"{request.topic} has important practical implications that most engineers overlook.",
            angle="deep-dive",
            reader_pain_point=f"Engineers struggle with {request.topic} without a clear mental model.",
            key_insight=f"Understanding {request.topic} deeply changes how you approach the problem.",
            hook_seed=f"You hit a wall with {request.topic}. The docs don't help. Here's why.",
            suggested_title=f"What Nobody Tells You About {request.topic}",
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    return await run_brief(request, anthropic_client)


async def _collect_evidence_spans(
    request: ArticleRequest,
    brief: StoryBrief | None,
    openai_client,
    debug_info: dict | None = None,
) -> list[EvidenceSpan]:
    """Collect evidence spans by running web searches, ranking results by
    trust score, and fetching the top-N URLs. When `debug_info` is provided,
    populates it with per-URL details so the SSE stage_completed event can
    show the user exactly which URLs were tried and what each contributed."""
    if not request.web_search or not _has_search_key():
        if debug_info is not None:
            debug_info["mode"] = "seed_only"
            debug_info["reason"] = (
                "web_search disabled" if not request.web_search
                else "no search API key configured"
            )
        return [_seed_evidence_span(request)]

    queries = await _generate_search_queries(request, brief, openai_client)
    search_results = await multi_search(queries)

    # Rank by trust score before fetching so we spend our fetch budget on the
    # highest-quality sources, not just whatever the search API returned first.
    ranked = sorted(search_results, key=lambda r: score_url(r.url), reverse=True)
    selected = ranked[:MAX_FETCH_URLS]
    span_groups = await asyncio.gather(
        *(process_search_result(result) for result in selected)
    )
    spans = [span for group in span_groups for span in group]

    # ── Fallback pass: triggered when initial fetch is thin ───────────
    # Count how many of the fetched URLs actually yielded content. If we
    # got fewer than MIN_USEFUL_SPANS chunks or fewer than MIN_USEFUL_SOURCES
    # URLs with content, the initial queries probably hit paywalls, 403s, or
    # returned poor results. Run a second pass with broader, topic-level
    # queries and exclude URLs we already tried.
    sources_yielded = sum(1 for g in span_groups if g)
    fallback_triggered = False
    fallback_spans_added = 0
    if len(spans) < MIN_USEFUL_SPANS or sources_yielded < MIN_USEFUL_SOURCES:
        already_tried = {r.url for r in selected}
        # Fallback queries: the raw topic plus one query per must_cover item
        # (no extra LLM call — these are always relevant by definition).
        fallback_queries: list[str] = [request.topic]
        fallback_queries += [
            f"{request.topic} {item}" for item in request.must_cover[:2]
        ]
        logging.info(
            "Initial search thin (%d spans, %d sources). "
            "Running fallback with %d broader queries.",
            len(spans), sources_yielded, len(fallback_queries),
        )
        fallback_results = await multi_search(fallback_queries)
        fresh = [r for r in fallback_results if r.url not in already_tried]
        if not fresh:
            logging.warning(
                "Fallback search returned no new URLs (all %d results already tried). "
                "Article may have limited source diversity.",
                len(fallback_results),
            )
            if debug_info is not None:
                debug_info["fallback_exhausted"] = True
        if fresh:
            ranked_fallback = sorted(
                fresh, key=lambda r: score_url(r.url), reverse=True
            )
            fallback_groups = await asyncio.gather(
                *(process_search_result(r) for r in ranked_fallback[:MAX_FETCH_URLS])
            )
            fallback_span_list = [s for g in fallback_groups for s in g]
            spans = spans + fallback_span_list
            fallback_triggered = True
            fallback_spans_added = len(fallback_span_list)
            logging.info(
                "Fallback search added %d new spans from %d URLs.",
                fallback_spans_added,
                sum(1 for g in fallback_groups if g),
            )

    if debug_info is not None:
        debug_info["queries"] = queries
        debug_info["search_results_total"] = len(search_results)
        debug_info["fetch_attempted"] = len(selected)
        debug_info["urls"] = [
            {
                "url": r.url,
                "title": r.title[:120] if r.title else "",
                "trust_score": round(score_url(r.url), 2),
                "spans_yielded": len(spans_for_url),
                "status": "fetched" if spans_for_url else "no_spans",
            }
            for r, spans_for_url in zip(selected, span_groups)
        ]
        # Note URLs that were ranked but didn't make the fetch cutoff —
        # gives the user visibility into what was skipped and why.
        debug_info["skipped_urls"] = [
            {
                "url": r.url,
                "title": r.title[:120] if r.title else "",
                "trust_score": round(score_url(r.url), 2),
                "reason": "below fetch budget",
            }
            for r in ranked[MAX_FETCH_URLS:]
        ][:5]  # cap at 5 to keep payload manageable
        if fallback_triggered:
            debug_info["fallback_triggered"] = True
            debug_info["fallback_spans_added"] = fallback_spans_added

    return spans or [_seed_evidence_span(request)]


async def _generate_search_queries(
    request: ArticleRequest,
    brief: StoryBrief | None,
    openai_client,
) -> list[str]:
    """Build the search-query list. Always includes a topic-level query plus
    one targeted query per must_cover item — those are free (no LLM call)
    and guarantee evidence for the aspects the user explicitly asked for.
    The LLM-generated queries fill the remaining slots so we still cover
    angles the user didn't think to name."""
    # Direct, no-LLM queries for explicit must_cover items. These are
    # cheap insurance: even if the LLM picks weird abstract queries, every
    # named aspect has at least one query targeting it directly.
    must_cover_queries = [
        f"{request.topic} {item}" for item in request.must_cover
    ]

    if isinstance(openai_client, MockOpenAIClient):
        base = [
            request.topic,
            f"{request.topic} best practices",
            f"{request.topic} examples",
        ]
        # Dedupe while preserving order.
        seen, merged = set(), []
        for q in must_cover_queries + base:
            if q not in seen:
                merged.append(q)
                seen.add(q)
        return merged

    # When must_cover is provided, take fewer LLM queries — the must_cover
    # queries already use most of the budget. Total stays at ~5 max.
    llm_query_count = max(1, 3 - len(must_cover_queries))

    user_content = f"topic: {request.topic}"
    if brief:
        user_content += (
            f"\nthesis: {brief.thesis}"
            f"\nangle: {brief.angle}"
            f"\nkey_insight: {brief.key_insight}"
        )
    if request.must_cover:
        user_content += (
            f"\nmust_cover (already-covered queries — do NOT duplicate): "
            f"{', '.join(request.must_cover)}"
        )

    completion = await openai_client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    f"Generate exactly {llm_query_count} targeted web search queries "
                    "that will find primary evidence for the given article thesis "
                    "and angle. Queries must be specific and focused — not generic "
                    "topic overviews. Avoid duplicating any must_cover queries "
                    "the caller already plans to run."
                ),
            },
            {"role": "user", "content": user_content},
        ],
        response_format=SearchQueries,
    )
    query_package = completion.choices[0].message.parsed
    if not isinstance(query_package, SearchQueries):
        query_package = SearchQueries.model_validate(query_package)

    llm_queries = query_package.queries[:llm_query_count]
    # Combine: must_cover-targeted queries FIRST so they're never starved
    # by a small total query budget downstream.
    seen, merged = set(), []
    for q in must_cover_queries + llm_queries:
        if q not in seen:
            merged.append(q)
            seen.add(q)
    return merged


async def _fill_evidence_gaps(
    plan: ArticlePlan,
    spans: list[EvidenceSpan],
    openai_client,
) -> list[EvidenceSpan]:
    """After planning, fetch evidence for any claims the planner couldn't ground."""
    if not _has_search_key():
        return spans

    gap_texts = find_evidence_gaps(plan, spans)
    if not gap_texts:
        return spans

    # Cap at 3 gap queries to avoid excessive API usage.
    queries = gap_texts[:3]
    logging.info("Evidence gaps detected for %d claim(s); running targeted searches.", len(queries))
    search_results = await multi_search(queries)
    if not search_results:
        return spans

    span_groups = await asyncio.gather(
        *(process_search_result(result) for result in search_results[:MAX_FETCH_URLS])
    )
    new_spans = [span for group in span_groups for span in group]
    return spans + new_spans


async def _generate_assets(plan: ArticlePlan, anthropic_client) -> list[RenderAsset]:
    """Render all visual intents (Mermaid diagrams and VHS GIFs) in parallel.

    Uses the same *anthropic_client* as the writing stages — either a native
    ``anthropic.AsyncAnthropic`` or the ``OpenAIAnthropicAdapter`` shim, so
    diagram generation works whether the user has an Anthropic or OpenAI key.
    """
    preset = plan.request.model_preset
    tasks = []
    for intent in plan.visual_intents:
        if intent.format == "vhs":
            tasks.append(process_vhs_intent(intent, anthropic_client, preset=preset))
        elif intent.format == "mermaid":
            tasks.append(process_visual_intent(intent, anthropic_client, preset=preset))

    if not tasks:
        return []

    return list(await asyncio.gather(*tasks))


def _openai_client(request: ArticleRequest):
    if os.environ.get("OPENAI_API_KEY"):
        return openai.AsyncOpenAI()
    return MockOpenAIClient(request)


def _resolve_provider() -> str:
    """Determine which LLM provider to use based on available keys.

    Rules (in priority order):
      1. Only OPENAI_API_KEY set        → "openai"  (auto)
      2. Only ANTHROPIC_API_KEY set     → "anthropic" (auto)
      3. Both set + LLM_PROVIDER pref  → honour pref (default "anthropic")
      4. Both set, no pref             → "anthropic"
      5. Neither set                   → "none"
    """
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai    = bool(os.environ.get("OPENAI_API_KEY"))

    if has_openai and not has_anthropic:
        return "openai"
    if has_anthropic and not has_openai:
        return "anthropic"
    if has_anthropic and has_openai:
        pref = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
        return pref if pref in ("anthropic", "openai") else "anthropic"
    return "none"


def _anthropic_client(request: ArticleRequest):
    """Return the writing-stage client, auto-selected from available keys.

    Provider priority:
      • Only OpenAI key present  → OpenAIAnthropicAdapter (no Claude credits needed)
      • Only Anthropic key       → anthropic.AsyncAnthropic
      • Both keys present        → use LLM_PROVIDER preference (default: Anthropic)
      • Neither key              → MockAnthropicClient (generates placeholder text)
    """
    provider = _resolve_provider()
    if provider == "openai":
        return OpenAIAnthropicAdapter(openai.AsyncOpenAI())
    if provider == "anthropic":
        return anthropic.AsyncAnthropic()
    logging.warning(
        "No LLM API key found (ANTHROPIC_API_KEY or OPENAI_API_KEY). "
        "Using mock client — add a key in Settings to generate real articles."
    )
    return MockAnthropicClient(request)


def _has_search_key() -> bool:
    return bool(
        os.environ.get("BRAVE_SEARCH_API_KEY")
        or os.environ.get("EXA_API_KEY")
        or os.environ.get("TAVILY_API_KEY")
    )


def _seed_evidence_span(request: ArticleRequest) -> EvidenceSpan:
    return EvidenceSpan(
        source_url="local://article-request",
        source_title="Article request",
        content=(
            f"The requested article topic is {request.topic}. "
            f"The article should target a {request.explanation_level} explanation "
            f"for {request.audience_role}s."
            + (f" Extra context: {request.extra_context}" if request.extra_context else "")
        ),
        trust_score=0.7,
    )


def _slug(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(part for part in slug.split("-") if part)[:80] or "article"


class MockOpenAIClient:
    def __init__(self, request: ArticleRequest) -> None:
        self.request = request
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=MockOpenAIParseCompletions(request))
        )
        self.chat = SimpleNamespace(completions=MockOpenAICreateCompletions(request))


class MockOpenAIParseCompletions:
    def __init__(self, request: ArticleRequest) -> None:
        self.request = request

    async def parse(self, **kwargs):
        response_format = kwargs["response_format"]
        if response_format is SearchQueries:
            parsed = SearchQueries(
                queries=[
                    self.request.topic,
                    f"{self.request.topic} best practices",
                    f"{self.request.topic} examples",
                ]
            )
        else:
            claim = _extract_claim(kwargs["messages"][-1]["content"])
            parsed = VerificationReport(
                claim_id=claim.get("claim_id", ""),
                support_status="supported",
                verifier_note="Mock verification used because API keys are absent.",
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )


class MockOpenAICreateCompletions:
    def __init__(self, request: ArticleRequest) -> None:
        self.request = request

    async def create(self, **kwargs):
        user_content = kwargs["messages"][-1]["content"]
        if user_content.startswith("{"):
            content = "flowchart LR\n  Request --> Plan\n  Plan --> Draft\n  Draft --> Verify"
        else:
            content = f"{self.request.topic} best practices"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class MockAnthropicClient:
    def __init__(self, request: ArticleRequest) -> None:
        self.request = request
        self.messages = MockAnthropicMessages(request)


class MockAnthropicMessages:
    def __init__(self, request: ArticleRequest) -> None:
        self.request = request

    async def create(self, **kwargs):
        # ── tool_use path: brief_worker (submit_story_brief) and
        #    editor_worker (submit_editor_report) both pass tool_choice={"type":"tool","name":...}
        #    and then iterate response.content looking for b.type == "tool_use".
        #    We must return a SimpleNamespace that matches that shape.
        tool_choice = kwargs.get("tool_choice")
        if isinstance(tool_choice, dict) and "name" in tool_choice:
            tool_name = tool_choice["name"]
            user_content = kwargs["messages"][-1]["content"]
            mock_input = self._mock_tool_input(tool_name, user_content)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name=tool_name,
                        input=mock_input,
                    )
                ]
            )

        # ── text path: planning, drafting, compiler, humanizer ───────────
        system = kwargs.get("system", "")
        user_content = kwargs["messages"][-1]["content"]

        if "planning brain" in system:
            content = _mock_plan_json(self.request, user_content)
        elif "writing one section" in system:
            content = _mock_section_markdown(user_content)
        elif "level compiler" in system:
            content = _mock_compiled_markdown(system, user_content)
        elif "copyeditor" in system:
            # Humanizer / polish: strip the prompt envelope and return just
            # the markdown content. Match is loosened from "final copyeditor"
            # to "copyeditor" so the combined polish prompt (which begins
            # "You are the final-pass copyeditor AND level-adapter") still
            # routes here.
            content = _mock_humanized_markdown(user_content)
        elif user_content.strip().startswith("{"):
            # Diagram intent JSON (from generate_spec via mermaid_worker /
            # vhs_worker). Return a minimal valid Mermaid flowchart so the
            # render step has something well-formed to work with in mock mode.
            content = (
                "flowchart LR\n"
                "  Request --> Plan\n"
                "  Plan --> Draft\n"
                "  Draft --> Verify"
            )
        else:
            content = user_content

        return SimpleNamespace(content=[SimpleNamespace(type="text", text=content)])

    def _mock_tool_input(self, tool_name: str, user_content: str = "") -> dict:
        """Return a mock dict that the matching Pydantic model can validate."""
        if tool_name == "submit_story_brief":
            return StoryBrief(
                thesis=(
                    f"{self.request.topic} has important practical implications "
                    "that most engineers overlook."
                ),
                angle="deep-dive",
                reader_pain_point=(
                    f"Engineers struggle with {self.request.topic} "
                    "without a clear mental model."
                ),
                key_insight=(
                    f"Understanding {self.request.topic} deeply "
                    "changes how you approach the problem."
                ),
                hook_seed=(
                    f"You hit a wall with {self.request.topic}. "
                    "The docs don't help. Here's why."
                ),
                suggested_title=f"What Nobody Tells You About {self.request.topic}",
            ).model_dump()
        if tool_name == "submit_editor_report":
            return EditorReport(
                approved=True,
                overall_assessment=(
                    "Mock editorial review: the draft meets quality standards."
                ),
                revisions=[],
            ).model_dump()
        if tool_name == "submit_article_plan":
            return self._mock_planner_output(user_content)
        if tool_name == "submit_clarification_questions":
            return self._mock_clarification_questions()
        if tool_name == "submit_topic_classification":
            return self._mock_topic_classification(user_content)
        if tool_name == "submit_relevance_check":
            return self._mock_relevance_check(user_content)
        if tool_name == "submit_critic_verdict":
            return self._mock_critic_verdict(user_content)
        # Unknown tool — return an empty dict; the caller's model_validate will
        # raise a clear Pydantic error rather than an obscure AttributeError.
        return {}

    def _mock_critic_verdict(self, user_content: str) -> dict:
        """Mock critic verdict. Default to approved=true so the mock pipeline
        runs to completion without triggering retries. Tests that exercise
        the refinement branch should monkeypatch the worker directly."""
        return {
            "approved": True,
            "issues": [],
            "overall_assessment": "Mock critic verdict: approved in mock mode.",
        }

    def _mock_relevance_check(self, user_content: str) -> dict:
        """Mock relevance verdict. Default to aligned=true so the mock pipeline
        runs to completion in tests and local dev. Tests that specifically
        exercise the misalignment branch should monkeypatch the worker, not
        rely on the mock LLM."""
        return {
            "aligned": True,
            "missing_aspects": [],
            "suggested_thesis_adjustment": "",
            "reasoning": "Mock relevance check — always aligned in mock mode.",
        }

    def _mock_clarification_questions(self) -> dict:
        """Mock structured clarification output. Used when API keys are absent
        so the /clarify and /generate endpoints still return a sensible shape
        in tests and local dev."""
        topic = self.request.topic
        return {
            "questions": [
                {
                    "id": "scope",
                    "question": f"Which aspect of {topic} should the article focus on?",
                    "options": [
                        f"{topic} fundamentals",
                        f"{topic} in production",
                        f"{topic} testing strategy",
                    ],
                },
                {
                    "id": "angle",
                    "question": "What angle do you want?",
                    "options": [
                        "fundamentals/explainer",
                        "tutorial: build something",
                        "war-story: real production lessons",
                    ],
                },
                {
                    "id": "must_cover",
                    "question": "Anything specific you want covered? (optional)",
                    "options": [],
                },
            ],
            "default_if_skipped": (
                f"explainer covering the foundational concepts of {topic}"
            ),
        }

    def _mock_topic_classification(self, user_content: str) -> dict:
        """Mock breadth classifier. Matches the Python heuristic when possible
        so test behavior is consistent between LLM-on and LLM-off paths.
        The Python heuristic should usually decide before the LLM is invoked,
        but the LLM fallback path needs this for ambiguous topics."""
        return {
            "breadth": "broad_defined",
            "reasoning": "Mock classification — defaulting to broad_defined.",
        }

    def _mock_planner_output(self, user_content: str) -> dict:
        """Mock structured planner output matching planning_worker._PlannerOutput."""
        topic = self.request.topic
        # Use the actual span_id from the prompt so downstream verification works.
        span_id = _first_span_id(user_content)
        return {
            "sections": [
                {
                    "title": "Why it matters",
                    "claim_ids": ["c1"],
                    "notes": "Explain the practical value.",
                    "narrative_note": (
                        "Opening section — build the hook around a concrete scenario."
                    ),
                },
                {
                    "title": "How to apply it",
                    "claim_ids": ["c1"],
                    "notes": "Give concrete steps.",
                    "narrative_note": (
                        "Middle section — show the mechanism through an example."
                    ),
                },
                {
                    "title": "Common pitfalls",
                    "claim_ids": ["c1"],
                    "notes": "Mention trade-offs.",
                    "narrative_note": (
                        "Closing section — end with what the reader should try next."
                    ),
                },
            ],
            "claims": [
                {
                    "id": "c1",
                    "text": (
                        f"{topic} benefits from clear goals, "
                        "small examples, and fast feedback."
                    ),
                    "source_ids": [span_id],
                    "freshness_sensitive": False,
                }
            ],
            "visual_intents": (
                [
                    {
                        "description": f"Pipeline for learning {topic}.",
                        "format": "mermaid",
                        "rationale": "A compact flow helps readers follow the process.",
                        "section_title": "Why it matters",
                    }
                ]
                if self.request.include_diagrams
                else []
            ),
            "evidence_span_ids": [span_id],
        }


def _mock_plan_json(request: ArticleRequest, user_content: str) -> str:
    span_id = _first_span_id(user_content)
    claim = Claim(
        text=f"{request.topic} benefits from clear goals, small examples, and fast feedback.",
        source_ids=[span_id],
    )
    sections = [
        ArticleSection(
            title="Why it matters",
            claim_ids=[str(claim.claim_id)],
            notes="Explain the practical value.",
            narrative_note="Opening section — build the hook around a concrete scenario.",
        ),
        ArticleSection(
            title="How to apply it",
            claim_ids=[str(claim.claim_id)],
            notes="Give concrete steps.",
            narrative_note="Middle section — show the mechanism through an example.",
        ),
        ArticleSection(
            title="Common pitfalls",
            claim_ids=[str(claim.claim_id)],
            notes="Mention trade-offs.",
            narrative_note="Closing section — end with what the reader should try next.",
        ),
    ]
    visual_intents = []
    if request.include_diagrams:
        visual_intents.append(
            VisualIntent(
                description=f"Pipeline for learning {request.topic}.",
                format="mermaid",
                rationale="A compact flow helps readers follow the process.",
                section_title="Why it matters",
            )
        )
    plan = ArticlePlan(
        request=request,
        sections=sections,
        claims=[claim],
        visual_intents=visual_intents,
        evidence_span_ids=[span_id],
    )
    return plan.model_dump_json()


def _mock_section_markdown(user_content: str) -> str:
    title = _extract_field(user_content, "section_title") or "Section"
    source_id = _first_source_id(user_content)
    topic = title.lower()
    return (
        f"## {title}\n\n"
        f"What changes when you treat {topic} as a habit instead of a checklist? "
        f"Small, repeatable examples make the idea easier to test [src:{source_id}]. "
        "That keeps the feedback loop short and makes mistakes cheaper to fix."
    )


def _mock_compiled_markdown(system: str, draft_markdown: str) -> str:
    level = _extract_system_value(system, "Level") or "intermediate"
    return (
        f"# {level.title()} Article\n\n"
        f"{draft_markdown}\n\n"
        "This article was generated with local mock LLM responses because API keys "
        "were not available."
    )


def _mock_humanized_markdown(user_content: str) -> str:
    """Strip the humanizer prompt envelope and return just the article markdown.

    The humanizer receives:
        article_thesis: ...\narticle_angle: ...\n...\n\narticle_markdown:\n<body>

    We extract everything after the `article_markdown:` marker so the mock
    does not leak the prompt header into the published article.
    """
    marker = "article_markdown:\n"
    if marker in user_content:
        return user_content.split(marker, 1)[1].strip()
    return user_content


def _extract_claim(content: str) -> dict:
    marker = "Claim:\n"
    if marker not in content:
        return {}
    raw = content.split(marker, 1)[1].split("\n\nEvidence:", 1)[0]
    return json.loads(raw)


def _first_span_id(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("[") and "] (" in line:
            return line[1 : line.index("]")]
    return str(_seed_evidence_span(ArticleRequest(topic="local")).span_id)


def _first_source_id(content: str) -> str:
    for line in content.splitlines():
        if '"source_ids":' in line:
            data = json.loads(content.split("claims_json:\n", 1)[1].split("\n\n", 1)[0])
            return data[0]["source_ids"][0]
    return "local-source"


def _extract_field(content: str, field: str) -> str:
    marker = f"{field}:\n"
    if marker not in content:
        return ""
    return content.split(marker, 1)[1].split("\n\n", 1)[0].strip()


def _extract_system_value(system: str, field: str) -> str:
    marker = f"{field}: "
    if marker not in system:
        return ""
    return system.split(marker, 1)[1].splitlines()[0].strip()


async def async_main() -> None:
    args = parse_args()
    if args.mode == "temporal":
        articles = await run_temporal(args)
    else:
        articles = await run_direct(args)

    paths = save_articles(articles, args.out)
    print_summary(articles, paths)
    requested_path = paths.get(args.level)
    if requested_path is not None:
        print(f"Output file path: {requested_path}")


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ModuleNotFoundError:
        pass

    asyncio.run(async_main())
