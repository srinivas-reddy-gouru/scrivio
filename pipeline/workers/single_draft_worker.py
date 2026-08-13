"""One call writes the whole article.

The relay this replaces drafted each section in its own LLM call and then
stitched the results with an editor pass and a polish pass. The project's
own matchup evals measured the cost of that structure: against a single
prompt to the same model, the relay lost every prose axis and the overall
verdict 6-0, and the judges' stated reasons were seam defects, sections
contradicting each other on design details and a per-section closing tic
that no single call would have produced.

What the relay had that a bare prompt does not is the research: searched,
trust-ranked, fact-checked evidence and a citation trail. That is the one
axis the pipeline won. So this worker keeps every research stage and
collapses only the writing: verified evidence in, complete article out.
"""
from __future__ import annotations

import logging

from pipeline.model_config import get_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    EvidenceSpan,
    PublishedArticle,
    RenderAsset,
)
from pipeline.workers._utils import extract_response_text
from pipeline.workers.citation_utils import scrub_em_dashes

_SINGLE_DRAFT_PROMPT = load_prompt("single_draft_v1.txt")

# One call carries the entire article, so it needs materially more room
# than a section call did.
_MAX_TOKENS = 16000


def _evidence_block(spans: list[EvidenceSpan], budget: int = 60) -> str:
    """Highest-trust spans first, capped so the prompt stays inside the
    context window. Sorting by trust means the cap drops the weakest
    evidence rather than whatever happened to be fetched last."""
    ranked = sorted(spans, key=lambda s: s.trust_score, reverse=True)[:budget]
    return "\n".join(
        f"[{span.span_id}] ({span.source_url}, trust {span.trust_score:.2f})\n"
        f"{span.content}\n---"
        for span in ranked
    )


def _outline_block(plan: ArticlePlan) -> str:
    return "\n".join(
        f"{i + 1}. {section.title}\n   goal: {section.narrative_note}"
        for i, section in enumerate(plan.sections)
    )


def _brief_block(plan: ArticlePlan) -> str:
    brief = plan.brief
    if brief is None:
        return f"thesis: {plan.request.topic}"
    return (
        f"thesis: {brief.thesis}\n"
        f"angle: {brief.angle}\n"
        f"reader's problem: {brief.reader_pain_point}\n"
        f"key insight: {brief.key_insight}\n"
        f"suggested title: {brief.suggested_title}"
    )


class SinglePassRejected(Exception):
    """The one-call draft came back unusable. Carries the defects so the
    caller can log why it fell back instead of silently degrading."""

    def __init__(self, defects: list[str]) -> None:
        super().__init__("; ".join(defects))
        self.defects = defects


def single_pass_defects(
    markdown: str,
    plan: ArticlePlan,
    spans: list[EvidenceSpan],
    stop_reason: str | None = None,
) -> list[str]:
    """Deterministic acceptance check on a one-call article.

    One call can fail in ways a section relay cannot: it can run out of
    output tokens halfway through, quietly cover three of six planned
    sections, or ignore the citation contract entirely. None of those are
    visible to the model itself, so they are checked here and the caller
    falls back to the relay rather than publishing a truncated article."""
    defects: list[str] = []
    text = markdown.strip()

    if stop_reason == "max_tokens":
        defects.append("output hit the token ceiling and stopped mid-article")
    elif text and text[-1] not in ".!?)`\"'":
        defects.append("article does not end on a finished sentence")

    words = len(text.split())
    if words < 500:
        defects.append(f"only {words} words, far short of an article")

    # Section coverage: headings the model actually wrote against the plan.
    written = sum(1 for line in text.splitlines() if line.startswith("## "))
    planned = len(plan.sections)
    if planned and written < max(2, int(planned * 0.6)):
        defects.append(f"covered {written} of {planned} planned sections")

    if spans and "[src:" not in text:
        defects.append("no citations, despite evidence being supplied")

    if text.count("```") % 2:
        defects.append("unclosed code fence")

    return defects


async def generate_whole_article(
    plan: ArticlePlan,
    spans: list[EvidenceSpan],
    request: ArticleRequest,
    client,
    assets: list[RenderAsset] | None = None,
) -> PublishedArticle:
    """Draft, adapt to level, and polish in one pass.

    Returns a PublishedArticle with [src:SPAN_ID] markers still in place;
    the caller runs the same deterministic gates the relay used (dash
    scrub, code-fence protection, citation resolution)."""
    diagram_block = ""
    rendered = [a for a in (assets or []) if a.qa_passed]
    if rendered:
        diagram_block = (
            "diagrams_available (drop each into the section where it explains "
            "the mechanism under discussion, as a fenced mermaid block):\n"
            + "\n".join(f"--- {a.spec.intent}\n{a.content}" for a in rendered)
            + "\n\n"
        )

    user_content = (
        f"brief:\n{_brief_block(plan)}\n\n"
        f"outline:\n{_outline_block(plan)}\n\n"
        f"{diagram_block}"
        f"evidence (cite by span id):\n{_evidence_block(spans)}"
    )
    system = _SINGLE_DRAFT_PROMPT.format(
        audience_role=request.audience_role,
        level=request.explanation_level,
    )

    response = await client.messages.create(
        model=get_model("drafting", request.model_preset),
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    markdown = scrub_em_dashes(extract_response_text(response).strip())
    defects = single_pass_defects(
        markdown, plan, spans, getattr(response, "stop_reason", None)
    )
    if not markdown:
        defects.insert(0, "empty response")
    if defects:
        # One retry on a leaner prompt: truncation and coverage failures are
        # usually an input-size problem, and half the evidence still leaves
        # the highest-trust spans in place.
        logging.warning("Single-pass draft rejected (%s); retrying leaner",
                        "; ".join(defects))
        retry = await client.messages.create(
            model=get_model("drafting", request.model_preset),
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": (
                f"brief:\n{_brief_block(plan)}\n\n"
                f"outline:\n{_outline_block(plan)}\n\n"
                f"evidence (cite by span id):\n{_evidence_block(spans, budget=25)}"
            )}],
        )
        markdown = scrub_em_dashes(extract_response_text(retry).strip())
        defects = single_pass_defects(
            markdown, plan, spans, getattr(retry, "stop_reason", None)
        )
        if defects:
            raise SinglePassRejected(defects)

    title = (plan.brief.suggested_title if plan.brief else "") or plan.request.topic
    for line in markdown.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    logging.info(
        "Single-pass article: %d words, %d evidence spans offered",
        len(markdown.split()), len(spans),
    )
    return PublishedArticle(
        request=request,
        title=title,
        markdown=markdown,
        assets=list(assets or []),
    )
