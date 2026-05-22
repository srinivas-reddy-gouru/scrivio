"""Critic agent: the final quality gate before publish.

Runs AFTER the polish stage on the final, embedded-diagrams, h1-prepended
article markdown. Critiques the article as a published unit — title,
opening, citations, diagrams, voice, consistency, structure — and decides
whether the article ships as-is or needs one more polish pass with
specific feedback injected.

Distinct from:
- the verifier, which checks individual CLAIMS against EVIDENCE
- the editor, which reads the DRAFT (before polish) and flags sections
  for revision before drafting completes
- the relevance checker, which only sees the brief vs. the request

The critic is the missing layer that reads the article AS THE READER
will see it and applies the polish-layer judgement no other agent does.
"""
from pathlib import Path

from pipeline.model_config import get_model
from pipeline.schemas.models import ArticlePlan, CriticVerdict


_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[1] / "prompts" / "critic_v1.txt"
).read_text(encoding="utf-8")


_CRITIC_TOOL: dict = {
    "name": "submit_critic_verdict",
    "description": (
        "Submit the editorial critique of the article — approved flag, "
        "list of specific issues with severity, and an overall assessment."
    ),
    "input_schema": CriticVerdict.model_json_schema(),
}


def _format_article_for_critic(article_markdown: str, plan: ArticlePlan) -> str:
    angle = plan.brief.angle if plan.brief else ""
    return (
        f"user_topic: {plan.request.topic}\n"
        f"user_extra_context: {plan.request.extra_context or '(none)'}\n"
        f"explanation_level: {plan.request.explanation_level}\n"
        f"article_angle: {angle or '(unknown)'}\n\n"
        f"article_markdown:\n{article_markdown}"
    )


async def critique_article(
    article_markdown: str,
    plan: ArticlePlan,
    client,
) -> CriticVerdict:
    """Ask the LLM to critique the polished article and return a structured
    verdict. The caller decides what to do with `blocking` issues."""
    user_content = _format_article_for_critic(article_markdown, plan)

    # Cache the system prompt: the critic runs twice when blocking issues are
    # found (humanizer fixes them, critic re-evaluates). Second call pays only
    # 10 % of the normal system-prompt token cost. Also benefits back-to-back
    # article runs within the 5-minute cache TTL.
    cached_system = [
        {"type": "text", "text": _SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral"}}
    ]
    response = await client.messages.create(
        model=get_model("critic", plan.request.model_preset),
        max_tokens=2048,
        system=cached_system,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        tools=[_CRITIC_TOOL],
        tool_choice={"type": "tool", "name": "submit_critic_verdict"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return CriticVerdict.model_validate(tool_use.input)
