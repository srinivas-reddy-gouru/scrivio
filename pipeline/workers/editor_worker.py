import asyncio
import logging
import random
import re

from pipeline.model_config import get_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    SectionRevision,
    ArticlePlan,
    DraftPackage,
    DraftSection,
    EditorReport,
    EvidenceSpan,
)
from pipeline.workers.drafting_worker import draft_section, get_relevant_spans


_SYSTEM_PROMPT = load_prompt("editor_v2.txt")

_EDITOR_TOOL: dict = {
    "name": "submit_editor_report",
    "description": "Submit the editorial review of the article draft.",
    "input_schema": EditorReport.model_json_schema(),
}


async def _call_with_rate_limit_retry(client, *, model, system, user_content, tools, tool_choice, max_retries=5):
    """Call Anthropic with exponential backoff + jitter on 429 rate-limit errors.

    The editor runs immediately after the drafter, which can deplete the
    30K-TPM bucket. Without retries a single 429 kills the entire job after
    all the expensive drafting work. With max_retries=5 the backoff sequence
    reaches 60 s (a full TPM-bucket reset window) before giving up:

        attempt 0 → sleep  1 s  (+jitter)
        attempt 1 → sleep  4 s  (+jitter)
        attempt 2 → sleep 16 s  (+jitter)
        attempt 3 → sleep 60 s  (+jitter)
        attempt 4 → raise

    Jitter (0–10 s) prevents concurrent retries from all hammering the API
    at the same instant.
    """
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(max_retries):
        try:
            return await client.messages.create(
                model=model,
                max_tokens=2048,
                system=system,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                tools=tools,
                tool_choice=tool_choice,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "rate_limit" not in msg and "429" not in msg:
                raise
            last_exc = exc
            if attempt < max_retries - 1:
                base = min(60, 4 ** attempt)
                jitter = random.uniform(0, 10)
                backoff = base + jitter
                logging.warning(
                    "Editor rate-limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1, max_retries, backoff,
                )
                await asyncio.sleep(backoff)
    raise last_exc


def _format_draft_for_review(draft: DraftPackage) -> str:
    parts = []
    for i, section in enumerate(draft.sections, start=1):
        parts.append(f"### Section {i}: {section.title}\n\n{section.content}")
    return "\n\n".join(parts)


async def run_editor_review(
    plan: ArticlePlan, draft: DraftPackage, client
) -> EditorReport:
    thesis = plan.brief.thesis if plan.brief else ""
    angle = plan.brief.angle if plan.brief else ""
    # The editor must check request-alignment FIRST (priority 0 in the prompt),
    # so the user's original topic and extra_context lead the user message.
    user_content = (
        f"user_topic: {plan.request.topic}\n"
        f"user_extra_context: {plan.request.extra_context or '(none)'}\n"
        f"article_thesis: {thesis}\n"
        f"article_angle: {angle}\n"
        f"audience: {plan.request.audience_role}\n"
        f"explanation_level: {plan.request.explanation_level}\n\n"
        f"draft:\n{_format_draft_for_review(draft)}"
    )
    # Cache the system prompt: if multiple articles are generated back-to-back,
    # calls 2+ pay only 10 % of the normal system-prompt input cost. The 25 %
    # write overhead on call 1 is recovered on any second call within 5 minutes.
    cached_system = [
        {"type": "text", "text": _SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral"}}
    ]
    response = await _call_with_rate_limit_retry(
        client,
        model=get_model("editor", plan.request.model_preset),
        system=cached_system,
        user_content=user_content,
        tools=[_EDITOR_TOOL],
        tool_choice={"type": "tool", "name": "submit_editor_report"},
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    report = EditorReport.model_validate(tool_use.input)
    # Deterministic supplement to the LLM review: citation coverage tends to
    # collapse in later sections and the model rarely notices. Cheap string
    # math catches it reliably.
    return flag_low_citation_sections(draft, report)


# ── Citation density (mechanical check) ──────────────────────────────

# Minimum fraction of prose paragraphs in a section that must carry at least
# one [src:...] citation marker. 0.4 because well-cited sections in real
# output land around 0.6-0.8 while degraded late sections drop to 0.0-0.2;
# 0.4 splits those populations without flagging legitimately narrative
# paragraphs (transitions, worked-example commentary) that need no citation.
CITATION_DENSITY_FLOOR = 0.4

# Sections that are mostly fenced code are walkthroughs — their prose
# narrates the code rather than asserting sourced facts. Above this ratio of
# code lines to total lines, the density check does not apply.
CODE_WALKTHROUGH_LINE_RATIO = 0.5

# Never mechanically flag more than this many sections per article: the
# drafter re-drafts every flagged section, and mass re-drafting on a thin
# evidence pool would churn the whole article for marginal benefit.
MAX_DENSITY_FLAGS = 2

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def section_citation_density(content: str) -> float | None:
    """Fraction of prose paragraphs containing a [src:...] marker.

    Returns None when the check does not apply: no prose paragraphs, or the
    section is predominantly a code walkthrough (see
    CODE_WALKTHROUGH_LINE_RATIO).
    """
    lines = content.splitlines()
    if lines:
        prose_line_count = len(_FENCE_RE.sub("", content).splitlines())
        if 1 - (prose_line_count / len(lines)) > CODE_WALKTHROUGH_LINE_RATIO:
            return None

    prose = _FENCE_RE.sub("", content)
    paragraphs = [
        p for p in (chunk.strip() for chunk in prose.split("\n\n"))
        # Headings, tables, diagram placeholders, and blank chunks are not
        # citable prose paragraphs.
        if p and not p.startswith(("#", "|", "<!--", "```"))
    ]
    if not paragraphs:
        return None
    cited = sum(1 for p in paragraphs if "[src:" in p)
    return cited / len(paragraphs)


def flag_low_citation_sections(
    draft: DraftPackage, report: EditorReport
) -> EditorReport:
    """Append revision requests for sections whose citation density is below
    CITATION_DENSITY_FLOOR. Sections the editor already flagged are skipped
    (one instruction per section keeps the re-draft focused)."""
    already_flagged = {rev.section_title for rev in report.revisions}
    added: list[SectionRevision] = []
    for section in draft.sections:
        if len(added) >= MAX_DENSITY_FLAGS:
            break
        if section.title in already_flagged:
            continue
        density = section_citation_density(section.content)
        if density is None or density >= CITATION_DENSITY_FLOOR:
            continue
        added.append(SectionRevision(
            section_title=section.title,
            issues=[
                f"Citation coverage is {density:.0%} of prose paragraphs, "
                f"below the {CITATION_DENSITY_FLOOR:.0%} floor."
            ],
            instruction=(
                "Add [src:...] citations from the provided evidence to the "
                "factual claims in this section; where no evidence supports "
                "a claim, hedge it or remove it."
            ),
        ))
        logging.info(
            "Citation density %.0f%% in section %r — flagged for revision.",
            density * 100, section.title,
        )

    if not added:
        return report
    # Low-density sections must actually trigger the revision pass, which
    # only runs when the report is not approved.
    return report.model_copy(update={
        "approved": False,
        "revisions": [*report.revisions, *added],
    })


async def revise_draft(
    plan: ArticlePlan,
    draft: DraftPackage,
    spans: list[EvidenceSpan],
    editor_report: EditorReport,
    client,
) -> DraftPackage:
    """Re-draft only the sections the editor flagged for revision or hinted for
    structural enhancement. Other sections pass through unchanged.

    Structural hints (e.g. "add a comparison table") are merged into the
    revision_note so the drafter can embed the enhancement naturally in the
    rewritten prose rather than appending it afterward.
    """
    flagged = {rev.section_title: rev for rev in editor_report.revisions}
    hinted = {h.section_title: h.hint for h in editor_report.structural_hints}

    if not flagged and not hinted:
        return draft

    thesis = plan.brief.thesis if plan.brief else ""
    total = len(plan.sections)
    new_sections: list[DraftSection] = []
    previous_summaries: list[str] = []
    existing_by_title = {s.title: s for s in draft.sections}

    for i, plan_section in enumerate(plan.sections):
        revision = flagged.get(plan_section.title)
        hint = hinted.get(plan_section.title)

        if revision is None and hint is None:
            existing = existing_by_title.get(plan_section.title)
            if existing is not None:
                new_sections.append(existing)
                preview = existing.content[:160].replace("\n", " ").strip()
                previous_summaries.append(f"{existing.title}: {preview}…")
                continue

        # Build a combined revision note: editorial instruction first, then any
        # structural enhancement hint on a clearly separated second line so the
        # drafter treats them as distinct directives rather than one long sentence.
        revision_note: str | None = revision.instruction if revision else None
        if hint:
            hint_directive = f"Structural enhancement to embed in this section: {hint}"
            revision_note = (
                f"{revision_note}\n\n{hint_directive}" if revision_note else hint_directive
            )

        if revision:
            logging.info("Editor revision for section %r: %s", plan_section.title, revision.instruction)
        if hint:
            logging.info("Editor structural hint for section %r: %s", plan_section.title, hint)

        relevant_spans = get_relevant_spans(plan_section, plan, spans)
        redrafted = await draft_section(
            plan_section,
            plan,
            relevant_spans,
            client,
            thesis=thesis,
            section_index=i,
            total_sections=total,
            previous_summaries=previous_summaries,
            revision_note=revision_note,
        )
        new_sections.append(redrafted)
        preview = redrafted.content[:160].replace("\n", " ").strip()
        previous_summaries.append(f"{redrafted.title}: {preview}…")

    raw_markdown = "\n\n".join(s.content for s in new_sections)
    return DraftPackage(plan=plan, sections=new_sections, raw_markdown=raw_markdown)
