"""Relevance-checker agent: validates that the brief will answer the user's request.

Runs immediately after the brief stage. If misaligned, the caller regenerates
the brief once with `missing_aspects` injected into extra_context. Cheap early
gate that prevents the expensive search/plan/draft stages from compounding
brief drift.
"""

from pipeline.model_config import get_model
from pipeline.prompt_loader import load_prompt
from pipeline.schemas.models import ArticleRequest, RelevanceCheck, StoryBrief


_SYSTEM_PROMPT = load_prompt("relevance_checker_v2.txt")


_RELEVANCE_TOOL: dict = {
    "name": "submit_relevance_check",
    "description": (
        "Submit the relevance verdict for the brief against the user's request."
    ),
    "input_schema": RelevanceCheck.model_json_schema(),
}


def _format_must_cover(must_cover: list[str]) -> str:
    if not must_cover:
        return "(none)"
    return ", ".join(must_cover)


def _format_brief(brief: StoryBrief) -> str:
    return (
        f"  thesis: {brief.thesis}\n"
        f"  angle: {brief.angle}\n"
        f"  reader_pain_point: {brief.reader_pain_point}\n"
        f"  key_insight: {brief.key_insight}\n"
        f"  hook_seed: {brief.hook_seed}\n"
        f"  suggested_title: {brief.suggested_title}"
    )


async def check_relevance(
    request: ArticleRequest,
    brief: StoryBrief,
    client,
) -> RelevanceCheck:
    """Ask the LLM whether the brief will produce an article the user wants.

    Returns a RelevanceCheck with `aligned` plus (when not aligned) the
    concrete `missing_aspects` and a suggested thesis rewrite.
    """
    user_content = (
        f"user_topic: {request.topic}\n"
        f"user_extra_context: {request.extra_context or '(none)'}\n"
        f"user_must_cover: {_format_must_cover(request.must_cover)}\n\n"
        f"brief:\n{_format_brief(brief)}"
    )

    # Haiku is sufficient for this binary alignment check (does brief match
    # user topic?). The task requires no creative reasoning — just comparing
    # two texts and producing a structured verdict. Haiku is ~12x cheaper than
    # Sonnet and fast, which matters here since this gate runs before every
    # expensive downstream stage.
    response = await client.messages.create(
        model=get_model("relevance", request.model_preset),
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=[_RELEVANCE_TOOL],
        tool_choice={"type": "tool", "name": "submit_relevance_check"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return RelevanceCheck.model_validate(tool_use.input)


def amend_request_with_missing_aspects(
    request: ArticleRequest,
    missing_aspects: list[str],
) -> ArticleRequest:
    """Inject relevance-checker feedback into the request's extra_context so
    the brief regeneration sees the missing aspects and the downstream stages
    (planner, drafter, editor) all see the same steering as well.

    Empty `missing_aspects` is a no-op — returns the request unchanged.
    """
    if not missing_aspects:
        return request

    aspect_block = (
        "relevance_checker_missing_aspects: "
        + ", ".join(missing_aspects)
        + " (these MUST be covered)"
    )
    if request.extra_context:
        new_context = f"{request.extra_context} | {aspect_block}"
    else:
        new_context = aspect_block

    return request.model_copy(update={"extra_context": new_context})
