import logging

from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    ArticlePlan,
    CriticIssue,
    DraftPackage,
    PublishedArticle,
    RenderAsset,
)
from pipeline.model_config import get_model
from pipeline.workers._utils import extract_response_text
# Imported from compiler_worker so we don't duplicate the placeholder-replacement
# regex. The function takes (markdown, assets) and returns markdown with each
# `<!-- DIAGRAM:UUID -->` swapped for a ```mermaid block.
from pipeline.workers.compiler_worker import _embed_diagrams

# Output token budget for the polish pass. claude-sonnet-4-6 supports up to
# 64K output tokens on the sync Messages API; articles typically run 4-12K
# output tokens depending on depth and section count. 16K gives 4x headroom
# over the previous 8K cap while staying well within the model's limit.
_MAX_OUTPUT_TOKENS = 16_000


_SYSTEM_PROMPT = load_prompt("polisher_v2.txt")


def _trim_to_last_clean_paragraph(text: str) -> str:
    """Trim a truncated article to the last complete paragraph boundary.

    Handles the three common truncation sites:
    - Mid-sentence inside prose  → strip back to the last sentence-ending `.`
    - Inside an unclosed code fence → close the fence then trim
    - Inside a mermaid block → same: close the fence then trim

    We keep at least 30 % of the text regardless — if the model was cut very
    early, we return what we have and let the caller's closing section handle it.
    """
    # Close any unclosed code fences (``` appears an odd number of times).
    if text.count("```") % 2 == 1:
        last_fence = text.rfind("```")
        text = text[:last_fence].rstrip()

    # Minimum position we're willing to trim back to (keep ≥ 30 % of content).
    min_keep = int(len(text) * 0.3)

    # Prefer the last paragraph break (double newline) anywhere past min_keep.
    last_para = text.rfind("\n\n")
    if last_para != -1 and last_para >= min_keep:
        return text[:last_para].rstrip()

    # Fall back to the last sentence-ending punctuation past min_keep.
    for pos in range(len(text) - 1, min_keep - 1, -1):
        if text[pos] in ".!?" and (pos + 1 >= len(text) or text[pos + 1] in (" ", "\n")):
            return text[:pos + 1].rstrip()

    # Nothing clean found — return as-is and let the closing section handle it.
    return text


async def _generate_closing_section(plan: ArticlePlan, polished_so_far: str, client, preset: str = "balanced") -> str:
    """Generate a brief closing when the article was truncated at the token limit.

    Uses Haiku (cheap, fast) since this is a small, low-stakes call — we just
    need 2-3 paragraphs that acknowledge what was covered and tell the reader
    what to explore next. No system prompt needed for a task this constrained.
    """
    section_titles = "\n".join(f"- {s.title}" for s in plan.sections)
    thesis = plan.brief.thesis if plan.brief else plan.request.topic

    # Identify which section headings actually appear in the text so we can
    # tell the model which parts were covered vs. which were cut.
    covered = [
        s.title for s in plan.sections
        if s.title in polished_so_far
    ]
    not_covered = [s.title for s in plan.sections if s.title not in covered]

    covered_block = "\n".join(f"- {t}" for t in covered) or "(unknown — article was short)"
    not_covered_block = "\n".join(f"- {t}" for t in not_covered) if not_covered else "(all sections were covered)"

    user_content = (
        f"An article on the following thesis was cut short due to length:\n"
        f"Thesis: {thesis}\n\n"
        f"Planned sections:\n{section_titles}\n\n"
        f"Sections that appear in the article text:\n{covered_block}\n\n"
        f"Sections that were NOT reached before the cut:\n{not_covered_block}\n\n"
        f"Write a closing section (150-250 words) that:\n"
        f"1. Briefly names what the article covered.\n"
        f"2. Points the reader toward the sections that were not covered, framing them "
        f"as natural next topics to explore.\n"
        f"3. Ends with one concrete action the reader can take today.\n\n"
        f"Rules: no 'In conclusion', no 'To summarize', no 'As we have seen'. "
        f"Write flowing prose as a senior technical editor would. "
        f"Return only the closing text, no heading."
    )
    response = await client.messages.create(
        model=get_model("closing", preset),
        max_tokens=400,
        messages=[{"role": "user", "content": user_content}],
    )
    return extract_response_text(response).strip()


def _format_critic_feedback(issues: list[CriticIssue]) -> str:
    """Render critic issues as instructions the humanizer can act on."""
    lines = []
    for i in issues:
        lines.append(
            f"- [{i.severity}] {i.category} at {i.location}: "
            f"{i.issue}  FIX: {i.fix}"
        )
    return "\n".join(lines)


async def humanize_markdown(
    markdown: str,
    plan: ArticlePlan,
    client,
    *,
    critic_feedback: list[CriticIssue] | None = None,
) -> str:
    thesis = plan.brief.thesis if plan.brief else ""
    angle = plan.brief.angle if plan.brief else ""

    # When the critic flagged blocking issues, inject them so the
    # humanizer's next pass addresses each one. We keep this block
    # separate from the rest of the prompt envelope so the model can
    # tell "this is a revision pass" vs "first pass".
    feedback_block = ""
    if critic_feedback:
        feedback_block = (
            "editorial_revisions_required:\n"
            f"{_format_critic_feedback(critic_feedback)}\n"
            "(This article was already polished once. A senior editor flagged "
            "the issues above. Address each one in this pass; do not rewrite "
            "the rest of the article.)\n\n"
        )

    user_content = (
        f"article_thesis: {thesis}\n"
        f"article_angle: {angle}\n"
        f"audience: {plan.request.audience_role}\n"
        f"explanation_level: {plan.request.explanation_level}\n\n"
        f"{feedback_block}"
        f"article_markdown:\n{markdown}"
    )
    # Cache the system prompt: on a critic-triggered second pass the humanizer
    # runs twice within 5 minutes; the second call pays only 10 % of the normal
    # system-prompt input cost. Write overhead is 25 % extra on the first call,
    # so caching is net-positive any time a second pass occurs (~50 % of runs).
    cached_system = [
        {"type": "text", "text": _SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral"}}
    ]
    preset = plan.request.model_preset
    response = await client.messages.create(
        model=get_model("polish", preset),
        max_tokens=_MAX_OUTPUT_TOKENS,
        system=cached_system,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        messages=[{"role": "user", "content": user_content}],
    )
    text = extract_response_text(response).strip()

    # If the model hit the token ceiling mid-article, recover gracefully:
    # trim to the last clean paragraph boundary, then generate a brief
    # closing section so the article ends properly rather than mid-sentence.
    if getattr(response, "stop_reason", None) == "max_tokens":
        logging.warning(
            "Humanizer hit max_tokens (%d) — trimming and generating closing section.",
            _MAX_OUTPUT_TOKENS,
        )
        text = _trim_to_last_clean_paragraph(text)
        closing = await _generate_closing_section(plan, text, client, preset)
        text = f"{text}\n\n{closing}"

    return text


async def humanize_article(
    article: PublishedArticle, plan: ArticlePlan, client
) -> PublishedArticle:
    rewritten = await humanize_markdown(article.markdown, plan, client)
    return article.model_copy(update={"markdown": rewritten})


async def polish_draft_to_article(
    draft: DraftPackage,
    plan: ArticlePlan,
    client,
    assets: list[RenderAsset] | None = None,
    *,
    critic_feedback: list[CriticIssue] | None = None,
    prior_markdown: str | None = None,
) -> PublishedArticle:
    """Single pass that replaces the old compile + humanize sequence.

    Previously the pipeline ran TWO full-article LLM calls back to back:
    `compile_level` rewrote the section drafts into "publication-ready"
    prose at the requested level, then `humanize_article` rewrote that
    output again for natural voice. Each call generated up to 16K tokens
    of output — together about 200-400 seconds of latency and double the
    cost per article. With the single-level fix from Sprint 6 they
    operated on the same content too.

    This combined pass tells one LLM call to do both jobs: adapt the
    draft to the requested explanation level AND polish its voice. The
    humanizer prompt has been extended with level-adaptation rules so
    a single model call covers what previously required two.

    Diagrams are still embedded the same way: the prompt is told to
    preserve `<!-- DIAGRAM:UUID -->` placeholders, and we replace them
    with rendered mermaid blocks after the LLM returns.
    """
    # On a refinement pass (critic flagged blocking issues), start from
    # the already-polished version. The humanizer's job becomes "address
    # these specific issues" rather than "rewrite from scratch". On a
    # first pass, start from the joined section drafts.
    if prior_markdown is not None:
        input_markdown = prior_markdown
    else:
        input_markdown = draft.raw_markdown or "\n\n".join(
            section.content for section in draft.sections
        )
    polished = await humanize_markdown(
        input_markdown, plan, client, critic_feedback=critic_feedback,
    )
    final_markdown = _embed_diagrams(polished, assets or [])

    brief = draft.plan.brief
    title = brief.suggested_title if brief else draft.plan.request.topic

    # Prepend the article title as an h1 if the polished markdown doesn't
    # already start with one. Standalone markdown rendered on GitHub,
    # Obsidian, etc. expects a top-level heading; section drafts start at
    # h2 by convention, so without this the published file looks like it's
    # missing its title.
    stripped = final_markdown.lstrip()
    if not stripped.startswith("# "):
        final_markdown = f"# {title}\n\n{final_markdown}"

    return PublishedArticle(
        request=draft.plan.request,
        title=title,
        markdown=final_markdown,
    )
