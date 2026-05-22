from pathlib import Path

import anthropic

from pipeline.model_config import get_model
from pipeline.schemas.models import ArticleRequest, StoryBrief


_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[1] / "prompts" / "brief_v1.txt"
).read_text(encoding="utf-8")

# Force structured output via tool_use — Anthropic's equivalent of OpenAI's
# response_format. tool_choice="tool" guarantees the model always calls this.
_BRIEF_TOOL: dict = {
    "name": "submit_story_brief",
    "description": "Submit the completed story brief for the article.",
    "input_schema": StoryBrief.model_json_schema(),
}


async def run_brief(
    request: ArticleRequest, client: anthropic.AsyncAnthropic
) -> StoryBrief:
    user_content = (
        f"topic: {request.topic}\n"
        f"explanation_level: {request.explanation_level}\n"
        f"audience_role: {request.audience_role}\n"
        f"extra_context: {request.extra_context or 'none'}"
    )
    # Cache the system prompt: if the relevance check sends the brief back for
    # regeneration (happens in ~20 % of runs), the second call reads the cached
    # system prompt at 10 % of the normal token cost. Write overhead is only
    # 25 % on the first call, so caching is net-positive even at low retry rates.
    cached_system = [
        {"type": "text", "text": _SYSTEM_PROMPT,
         "cache_control": {"type": "ephemeral"}}
    ]
    response = await client.messages.create(
        model=get_model("brief", request.model_preset),
        max_tokens=1024,
        system=cached_system,
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        tools=[_BRIEF_TOOL],
        tool_choice={"type": "tool", "name": "submit_story_brief"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return StoryBrief.model_validate(tool_use.input)
