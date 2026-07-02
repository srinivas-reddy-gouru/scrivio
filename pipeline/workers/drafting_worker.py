import asyncio
import logging
import random
import re

from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    ArticlePlan,
    ArticleSection,
    DraftPackage,
    DraftSection,
    EvidenceSpan,
)
from pipeline.workers._utils import extract_response_text


_SYSTEM_PROMPT = load_prompt("drafter_v2.txt")

CITATION_PATTERN = re.compile(r"\[src:([^\]]+)\]")

# Drafter model. Sonnet 4.6 is ~3-5x faster than Opus 4.7 with quality that's
# more than adequate for per-section technical writing where structure and
# accuracy matter more than literary flourish. The editor stage will catch
# anything weak.
_DRAFTER_MODEL = "claude-sonnet-4-6"

# Cap on parallel section drafts. Lowered from 6 to 3 because tier-1 Anthropic
# accounts default to 30K input-tokens-per-minute on Sonnet; each section
# draft uses ~8K input tokens (system prompt + outline + evidence + claims),
# so 6 in flight at once = ~48K tokens, instant 429. With concurrency=3 we
# stay under 30K per minute even with prompt-cache misses, and the 429-retry
# wrapper below handles transient bursts when the cap is briefly exceeded.
_DRAFT_CONCURRENCY = 3
_draft_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _draft_semaphore
    if _draft_semaphore is None:
        _draft_semaphore = asyncio.Semaphore(_DRAFT_CONCURRENCY)
    return _draft_semaphore


async def _call_with_rate_limit_retry(
    client, *, model: str, max_tokens: int, system: str, user_content: str,
    max_retries: int = 5,
):
    """Call Anthropic with exponential backoff + jitter on 429 rate-limit errors.

    Anthropic's per-minute token budget resets every 60 seconds.  We need the
    backoff sequence to reach at least 60 s so the bucket fully refills before
    the last retry.  With max_retries=5 the base sequence is:

        attempt 0 → sleep  1 s  (+jitter)
        attempt 1 → sleep  4 s  (+jitter)
        attempt 2 → sleep 16 s  (+jitter)
        attempt 3 → sleep 60 s  (+jitter)
        attempt 4 → raise

    Jitter (0–10 s uniform) is added to every sleep.  Without it, parallel
    section drafts that all hit the limit at the same moment will retry at the
    same moment, producing a thundering-herd loop that exhausts all retries
    without ever clearing the window.  The jitter spreads concurrent retries
    across a 10-second window so they land in separate TPM slices.

    Only 429 / rate_limit errors are retried; all other errors propagate
    immediately.
    """
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(max_retries):
        try:
            # Pass the system prompt as a cacheable block. The drafter is
            # called 6–9 times per pipeline run (6 initial sections + up to 3
            # editor revisions). Calls 2–9 pay only 10 % of the normal token
            # price for the cached portion, saving ~75 % on system-prompt
            # input tokens across the batch. Requires >= 1024 tokens in the
            # cached block; this system prompt is ~3 500 tokens.
            cached_system = [
                {"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}
            ]
            return await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=cached_system,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as exc:  # noqa: BLE001 — RateLimitError is upstream class
            msg = str(exc).lower()
            if "rate_limit" not in msg and "429" not in msg:
                raise
            last_exc = exc
            if attempt < max_retries - 1:
                base = min(60, 4 ** attempt)          # 1, 4, 16, 60
                jitter = random.uniform(0, 10)         # spread concurrent retries
                backoff = base + jitter
                logging.warning(
                    "Drafter rate-limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1, max_retries, backoff,
                )
                await asyncio.sleep(backoff)
    raise last_exc


def get_relevant_spans(
    section: ArticleSection, plan: ArticlePlan, all_spans: list[EvidenceSpan]
) -> list[EvidenceSpan]:
    section_claim_ids = set(section.claim_ids)
    source_ids = {
        source_id
        for claim in plan.claims
        if str(claim.claim_id) in section_claim_ids
        for source_id in claim.source_ids
    }

    return [span for span in all_spans if str(span.span_id) in source_ids]


async def draft_section(
    section: ArticleSection,
    plan: ArticlePlan,
    spans: list[EvidenceSpan],
    client,
    *,
    thesis: str = "",
    section_index: int = 0,
    total_sections: int = 1,
    previous_summaries: list[str] | None = None,  # legacy; ignored in parallel mode
    article_outline: str = "",
    revision_note: str | None = None,
) -> DraftSection:
    claims = [
        claim for claim in plan.claims if str(claim.claim_id) in section.claim_ids
    ]
    claims_json = "[" + ",".join(claim.model_dump_json() for claim in claims) + "]"
    evidence_context = "\n".join(
        f"[{span.span_id}] ({span.source_url})\n{span.content}\n---"
        for span in spans
    )

    # `article_outline` replaces `previous_summaries`: instead of waiting
    # for prior sections to draft so we can summarize them, we pass the
    # FULL article structure (titles + notes) to every section up front.
    # Each section can write transitions referencing what comes before
    # AND after without serializing the calls.
    outline_block = ""
    if article_outline:
        outline_block = f"article_outline:\n{article_outline}\n\n"
    elif previous_summaries:
        # Backward compat for any caller still passing this.
        summaries = "\n".join(f"  - {s}" for s in previous_summaries)
        outline_block = f"previous_sections:\n{summaries}\n\n"

    revision_block = ""
    if revision_note:
        revision_block = (
            f"EDITOR REVISION — THIS SECTION MUST BE REWRITTEN:\n"
            f"The previous draft was rejected. The editor's instruction below is a precise command, not a suggestion.\n"
            f"Read the instruction carefully. Execute it literally.\n"
            f"  - If it says 'cut bullet lists' → your revised section must contain no bullet lists.\n"
            f"  - If it says 'rewrite the opening to lead with X' → your revised section must open with X.\n"
            f"  - If it says 'replace with prose' → use prose, not lists.\n"
            f"Do not preserve the previous draft's structure unless the editor explicitly tells you to keep it.\n\n"
            f"Editor's instruction: {revision_note}\n\n"
        )

    angle = plan.brief.angle if plan.brief else "explainer"
    user_prompt = (
        f"user_topic: {plan.request.topic}\n"
        f"user_extra_context: {plan.request.extra_context or '(none)'}\n"
        f"article_thesis: {thesis}\n"
        f"article_angle: {angle}\n"
        f"section_position: {section_index + 1} of {total_sections}\n"
        f"is_opening_section: {str(section_index == 0).lower()}\n"
        f"is_closing_section: {str(section_index == total_sections - 1).lower()}\n"
        f"{outline_block}"
        f"{revision_block}"
        f"narrative_note: {section.narrative_note or 'none provided'}\n\n"
        f"section_title:\n{section.title}\n\n"
        f"section_notes:\n{section.notes}\n\n"
        f"claims_json:\n{claims_json}\n\n"
        f"evidence:\n{evidence_context}"
    )

    async with _get_semaphore():
        response = await _call_with_rate_limit_retry(
            client,
            model=_DRAFTER_MODEL,
            max_tokens=8192,
            system=_SYSTEM_PROMPT,
            user_content=user_prompt,
        )
    content = extract_response_text(response).strip()

    # If the planner attached any visual_intents to this section, append a
    # placeholder for each one. The compiler will replace these with rendered
    # mermaid fenced blocks once the assets are available.
    section_intents = [
        intent
        for intent in plan.visual_intents
        if intent.section_title == section.title
    ]
    if section_intents:
        placeholders = "\n\n".join(
            f"<!-- DIAGRAM:{intent.intent_id} -->" for intent in section_intents
        )
        content = f"{content}\n\n{placeholders}"

    citation_ids = CITATION_PATTERN.findall(content)

    return DraftSection(
        title=section.title,
        content=content,
        citation_ids=citation_ids,
    )


async def draft_all_sections(
    plan: ArticlePlan, spans: list[EvidenceSpan], client
) -> DraftPackage:
    """Draft every section IN PARALLEL.

    Previously this was a sequential loop where each section waited for
    previous_summaries to be populated. With Sonnet at ~30s/section and
    8 sections, that put drafting on the critical path for ~4 minutes.
    We now pass the full article outline (titles + notes) to every section
    up front and run them concurrently, capped by `_draft_semaphore` to
    avoid bursting the per-minute token limit.
    """
    thesis = plan.brief.thesis if plan.brief else ""
    total = len(plan.sections)

    # Build the full outline once and share it across every section. Each
    # drafter sees the whole article structure and can write transitions
    # without depending on prior section text.
    outline_lines = []
    for i, sec in enumerate(plan.sections):
        notes = (sec.notes or "").strip().replace("\n", " ")
        outline_lines.append(
            f"  {i+1}. {sec.title}"
            + (f" — {notes}" if notes else "")
        )
    article_outline = "\n".join(outline_lines)

    async def _draft_one(i: int, section: ArticleSection) -> DraftSection:
        relevant_spans = get_relevant_spans(section, plan, spans)
        return await draft_section(
            section,
            plan,
            relevant_spans,
            client,
            thesis=thesis,
            section_index=i,
            total_sections=total,
            article_outline=article_outline,
        )

    section_drafts = list(
        await asyncio.gather(
            *(_draft_one(i, sec) for i, sec in enumerate(plan.sections))
        )
    )

    raw_markdown = "\n\n".join(s.content for s in section_drafts)
    return DraftPackage(plan=plan, sections=section_drafts, raw_markdown=raw_markdown)
