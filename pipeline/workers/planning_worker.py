import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from pipeline.model_config import get_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    ArticleSection,
    Claim,
    EvidenceSpan,
    StoryBrief,
    VisualIntent,
)


_SYSTEM_PROMPT = load_prompt("planner_v2.txt")


class PlannerError(Exception):
    pass


# Intermediate schema for the planner's structured tool_use output. Using short
# string IDs (c1, c2, ...) for claims instead of UUIDs — LLMs reliably maintain
# short ID references across a JSON document but struggle with consistent UUIDs.
# We map these to real UUID-backed Claim objects after parsing.
class _PlannerClaim(BaseModel):
    id: str = Field(description="Short claim ID like 'c1', 'c2'. Referenced by sections.claim_ids.")
    text: str = Field(description="Single, specific, falsifiable assertion.")
    source_ids: list[str] = Field(description="Real span_ids from evidence_spans. Never invent.")
    freshness_sensitive: bool = False


class _PlannerSection(BaseModel):
    title: str
    claim_ids: list[str] = Field(description="IDs from the claims array, e.g. ['c1', 'c3'].")
    notes: str = ""
    narrative_note: str = Field(description="Tension this section resolves and structural cue for the drafter.")


class _PlannerVisualIntent(BaseModel):
    description: str
    format: Literal["mermaid", "graphviz", "vhs"] = "mermaid"
    rationale: str
    section_title: str = Field(
        description=(
            "Exact title of the section this diagram belongs in. Must match "
            "one of the section titles in the sections array. The drafter will "
            "embed the diagram inside that section's markdown."
        )
    )


class _PlannerOutput(BaseModel):
    sections: list[_PlannerSection]
    claims: list[_PlannerClaim]
    visual_intents: list[_PlannerVisualIntent] = []
    evidence_span_ids: list[str] = Field(description="span_ids cited anywhere in claims.")


_PLAN_TOOL: dict = {
    "name": "submit_article_plan",
    "description": "Submit the completed article plan with sections, claims, and citations.",
    "input_schema": _PlannerOutput.model_json_schema(),
}


# Feed only the highest-trust spans to the planner. Beyond ~40 spans the
# marginal evidence is low-value and the JSON plan starts to exceed safe
# max_tokens limits. The gap-fill pass runs afterward to patch specific holes.
MAX_PLANNER_SPANS = 40
# Max chunks taken from any single source URL. The tight cap (2 instead of
# the original 5) forces the planner to see evidence from at least 20
# distinct URLs. This is the source-quality lever: one long article on a
# high-trust domain can no longer monopolize the plan, so the resulting
# article cites a more diverse set of sources.
MAX_SPANS_PER_URL = 2


def _select_planner_spans(spans: list) -> list:
    """Return up to MAX_PLANNER_SPANS with at most MAX_SPANS_PER_URL per source.

    Strategy:
    1. Sort sources by trust_score (best URLs first).
    2. Round-robin across sources, taking up to MAX_SPANS_PER_URL chunks each,
       until MAX_PLANNER_SPANS is reached.

    This ensures the planner sees evidence from multiple sources rather than
    all 40 slots going to a single high-trust domain.
    """
    from collections import defaultdict

    # Group spans by URL, preserving document order within each group.
    by_url: dict[str, list] = defaultdict(list)
    for span in spans:
        by_url[span.source_url].append(span)

    # Rank URLs by their trust_score (all chunks from a URL share the score).
    ranked_urls = sorted(
        by_url.keys(),
        key=lambda url: by_url[url][0].trust_score,
        reverse=True,
    )

    selected: list = []
    url_counts: dict[str, int] = defaultdict(int)

    # Round-robin: one chunk per URL per pass until budget is exhausted.
    changed = True
    while changed and len(selected) < MAX_PLANNER_SPANS:
        changed = False
        for url in ranked_urls:
            if len(selected) >= MAX_PLANNER_SPANS:
                break
            if url_counts[url] >= MAX_SPANS_PER_URL:
                continue
            idx = url_counts[url]
            if idx < len(by_url[url]):
                selected.append(by_url[url][idx])
                url_counts[url] += 1
                changed = True

    return selected


def filter_spans_by_age(
    spans: list[EvidenceSpan], max_age_days: int
) -> list[EvidenceSpan]:
    """Remove spans older than max_age_days. Spans with no published_at are kept."""
    if max_age_days <= 0:
        return spans
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    fresh, stale = [], []
    for span in spans:
        if span.published_at is None:
            fresh.append(span)
        elif span.published_at.replace(tzinfo=timezone.utc) >= cutoff:
            fresh.append(span)
        else:
            stale.append(span)
    if stale:
        logging.info(
            "Age filter: kept %d spans, dropped %d older than %d days.",
            len(fresh),
            len(stale),
            max_age_days,
        )
    # If filtering removed everything, fall back to all spans so the planner
    # isn't left with nothing to work from.
    return fresh or spans


_PLANNER_EXCERPT_CHARS = 350  # per span, for the planning context only
# The planner needs span IDs + enough text to know what a source covers so it
# can write claims with source_ids. It does NOT need the full 800-char chunk —
# that level of detail is for the verifier and drafter. A 350-char excerpt
# (~2-3 sentences) is enough to identify what each span supports and cuts
# planning-context token cost by ~55 %.


def load_evidence_context(
    spans: list[EvidenceSpan],
    max_chars: int = 60000,
    excerpt_chars: int | None = None,
) -> str:
    """Build the evidence block for the planner.

    When `excerpt_chars` is set, each span's content is truncated to that
    many characters before being included. Use this for the planning stage
    where the full span is unnecessary; leave it as None for stages that need
    the full text (verifier, drafter).
    """
    def _content(span: EvidenceSpan) -> str:
        text = span.content
        if excerpt_chars is not None and len(text) > excerpt_chars:
            text = text[:excerpt_chars].rstrip() + "…"
        return text

    entries = [
        f"[{span.span_id}] ({span.source_url})\n{_content(span)}\n---"
        for span in sorted(spans, key=lambda span: span.trust_score, reverse=True)
    ]

    context_parts = []
    current_size = 0
    for entry in entries:
        separator_size = 1 if context_parts else 0
        next_size = current_size + separator_size + len(entry)

        if next_size <= max_chars:
            context_parts.append(entry)
            current_size = next_size
            continue

        remaining = max_chars - current_size - separator_size
        if remaining > 0:
            context_parts.append(entry[:remaining])
        break

    return "\n".join(context_parts)


def find_evidence_gaps(
    plan: ArticlePlan, spans: list[EvidenceSpan]
) -> list[str]:
    """Return claim texts whose source_ids don't match any span we actually have.

    The planner may reference span IDs that it hallucinated or that were never
    fetched. These claims will fail verification. Better to detect them before
    drafting and fetch targeted evidence first.
    """
    available_ids = {str(span.span_id) for span in spans}
    gap_texts = []
    for claim in plan.claims:
        if not any(sid in available_ids for sid in claim.source_ids):
            gap_texts.append(claim.text)
    return gap_texts


async def run_planner(
    request: ArticleRequest,
    spans: list[EvidenceSpan],
    client,
    brief: StoryBrief | None = None,
) -> ArticlePlan:
    # Apply age filter, then select a diversity-aware subset for the planner.
    filtered_spans = filter_spans_by_age(spans, request.max_source_age_days)
    top_spans = _select_planner_spans(filtered_spans)
    if len(filtered_spans) > len(top_spans):
        logging.info(
            "Planner span cap: selected %d of %d spans (%d sources, max %d per source).",
            len(top_spans),
            len(filtered_spans),
            len({s.source_url for s in top_spans}),
            MAX_SPANS_PER_URL,
        )
    evidence_context = load_evidence_context(
        top_spans, excerpt_chars=_PLANNER_EXCERPT_CHARS
    )

    brief_block = ""
    if brief:
        brief_block = (
            f"story_brief:\n"
            f"  thesis: {brief.thesis}\n"
            f"  angle: {brief.angle}\n"
            f"  reader_pain_point: {brief.reader_pain_point}\n"
            f"  key_insight: {brief.key_insight}\n"
            f"  hook_seed: {brief.hook_seed}\n\n"
        )

    extra_block = (
        f"\nextra_context: {request.extra_context}" if request.extra_context else ""
    )

    user_prompt = (
        f"{brief_block}"
        f"article_request:\n{request.model_dump_json()}{extra_block}\n\n"
        f"evidence_spans:\n{evidence_context}"
    )

    response = await client.messages.create(
        model=get_model("planning", request.model_preset),
        max_tokens=8192,
        system=_SYSTEM_PROMPT,
        tools=[_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "submit_article_plan"},
        messages=[{"role": "user", "content": user_prompt}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    output = _PlannerOutput.model_validate(tool_use.input)

    plan = _build_article_plan(output, request, brief)

    empty_source_claims = [
        str(claim.claim_id) for claim in plan.claims if not claim.source_ids
    ]
    if empty_source_claims:
        raise PlannerError(
            "Planner returned claims without source_ids: "
            + ", ".join(empty_source_claims)
        )

    return plan


def _build_article_plan(
    output: _PlannerOutput,
    request: ArticleRequest,
    brief: StoryBrief | None,
) -> ArticlePlan:
    """Translate the planner's short-ID schema into a fully-typed ArticlePlan.

    Maps each _PlannerClaim's short id (e.g. "c1") to a real Claim with a
    freshly-generated UUID, then rewrites every section's claim_ids to point
    at those UUIDs.
    """
    claims: list[Claim] = []
    id_to_uuid: dict[str, str] = {}
    for pc in output.claims:
        claim = Claim(
            text=pc.text,
            source_ids=pc.source_ids,
            freshness_sensitive=pc.freshness_sensitive,
        )
        id_to_uuid[pc.id] = str(claim.claim_id)
        claims.append(claim)

    sections: list[ArticleSection] = []
    for ps in output.sections:
        # If the model returned an unknown claim ref (e.g. "c99"), drop it
        # rather than crash. Verification will then flag unsupported claims.
        mapped = [id_to_uuid[cid] for cid in ps.claim_ids if cid in id_to_uuid]
        sections.append(
            ArticleSection(
                title=ps.title,
                claim_ids=mapped,
                notes=ps.notes,
                narrative_note=ps.narrative_note,
            )
        )

    # Carry section_title through, but only if it matches a real section.
    # An invalid section_title becomes None — the diagram is then orphaned
    # rather than wrongly attached, which is the safer failure mode.
    valid_section_titles = {s.title for s in sections}
    visual_intents = [
        VisualIntent(
            description=vi.description,
            format=vi.format,
            rationale=vi.rationale,
            section_title=vi.section_title if vi.section_title in valid_section_titles else None,
        )
        for vi in output.visual_intents
    ]

    return ArticlePlan(
        request=request,
        brief=brief,
        sections=sections,
        claims=claims,
        visual_intents=visual_intents,
        evidence_span_ids=output.evidence_span_ids,
    )


