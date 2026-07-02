import re

from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import (
    DraftPackage,
    ExplanationLevel,
    PublishedArticle,
    RenderAsset,
)
from pipeline.workers._utils import extract_response_text


_SYSTEM_PROMPT_TEMPLATE = load_prompt("compiler_v2.txt")

CITATION_PATTERN = re.compile(r"\[src:([^\]]+)\]")
DIAGRAM_PLACEHOLDER_PATTERN = re.compile(r"<!-- DIAGRAM:([^\s-][^\s]*) -->")
LEVELS: tuple[ExplanationLevel, ...] = ("basic", "intermediate", "advanced")


async def compile_level(
    draft: DraftPackage,
    level: ExplanationLevel,
    client,
    assets: list[RenderAsset] | None = None,
) -> PublishedArticle:
    brief = draft.plan.brief
    thesis_block = f"\nArticle thesis: {brief.thesis}" if brief else ""
    angle = brief.angle if brief else "explainer"

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        level=level,
        audience_role=draft.plan.request.audience_role,
        thesis_block=thesis_block,
        angle=angle,
    )
    original_markdown = draft.raw_markdown or "\n\n".join(
        section.content for section in draft.sections
    )
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=system_prompt,
        extra_headers={"anthropic-beta": "output-128k-2025-02-19"},
        messages=[{"role": "user", "content": original_markdown}],
    )
    markdown = extract_response_text(response).strip()
    missing_citation_ids = _missing_citation_ids(draft, markdown)

    if missing_citation_ids:
        markdown = (
            f"{markdown}\n\n"
            "<!-- WARNING: missing citations from original draft: "
            f"{', '.join(missing_citation_ids)} -->"
        )

    # Replace <!-- DIAGRAM:{intent_id} --> markers with rendered mermaid blocks.
    # Done AFTER citation-warning so warnings don't get treated as placeholders.
    markdown = _embed_diagrams(markdown, assets or [])

    title = brief.suggested_title if brief else draft.plan.request.topic

    return PublishedArticle(
        request=draft.plan.request,
        title=title,
        markdown=markdown,
    )


async def compile_all_levels(
    draft: DraftPackage,
    client,
    assets: list[RenderAsset] | None = None,
    levels: tuple[ExplanationLevel, ...] | None = None,
) -> dict[ExplanationLevel, PublishedArticle]:
    """Compile the draft for the requested levels (default: all three).

    Callers that only need a single level (the common case — the user
    asked for one) should pass `levels=(request.explanation_level,)`.
    Each level is an independent LLM call; running fewer levels is a
    near-linear cost saving on both the compile and humanize stages.
    """
    import asyncio
    target_levels = levels if levels is not None else LEVELS
    articles = await asyncio.gather(
        *(compile_level(draft, level, client, assets=assets) for level in target_levels)
    )
    return dict(zip(target_levels, articles))


def _embed_diagrams(markdown: str, assets: list[RenderAsset]) -> str:
    """Replace each `<!-- DIAGRAM:{intent_id} -->` placeholder with the
    matching asset's mermaid block. Placeholders with no matching asset (e.g.
    the asset failed to render) are removed silently rather than left as
    HTML comments in the published markdown.
    """
    by_intent_id = {str(asset.intent.intent_id): asset for asset in assets}

    def replace(match: re.Match[str]) -> str:
        intent_id = match.group(1)
        asset = by_intent_id.get(intent_id)
        if asset is None or not asset.spec:
            return ""
        fence_lang = asset.intent.format if asset.intent.format in ("mermaid", "graphviz") else "mermaid"
        return f"```{fence_lang}\n{asset.spec.strip()}\n```"

    result = DIAGRAM_PLACEHOLDER_PATTERN.sub(replace, markdown)
    # Collapse triple-blank-lines left behind when a placeholder was removed.
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _missing_citation_ids(draft: DraftPackage, markdown: str) -> list[str]:
    original_markdown = draft.raw_markdown or "\n\n".join(
        section.content for section in draft.sections
    )
    citation_ids = []
    citation_ids.extend(CITATION_PATTERN.findall(original_markdown))
    for section in draft.sections:
        citation_ids.extend(section.citation_ids)

    expected_ids = list(dict.fromkeys(citation_ids))
    found_ids = set(CITATION_PATTERN.findall(markdown))
    return [citation_id for citation_id in expected_ids if citation_id not in found_ids]
