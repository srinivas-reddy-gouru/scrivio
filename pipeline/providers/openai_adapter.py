"""OpenAI → Anthropic shim.

Wraps an ``openai.AsyncOpenAI`` client so it presents the same interface that
every pipeline worker expects from ``anthropic.AsyncAnthropic``.

Why a shim instead of per-worker branches?
  Workers call ``client.messages.create(**kwargs)`` using Anthropic's argument
  shape and read Anthropic-shaped responses.  Adding an "if openai / if
  anthropic" branch to each of the 6+ workers would scatter provider logic
  everywhere.  A single adapter keeps every worker unchanged.

Conversions handled:
  • System prompt  — Anthropic ``system`` param (string or cached block list)
                     → OpenAI system message
  • Tools          — Anthropic ``{"name","description","input_schema"}``
                     → OpenAI ``{"type":"function","function":{...}}``
  • tool_choice    — Anthropic ``{"type":"tool","name":"X"}``
                     → OpenAI ``{"type":"function","function":{"name":"X"}}``
  • Response       — OpenAI ``choices[0].message`` wrapped to look like
                     ``SimpleNamespace(content=[...], stop_reason=...)``
  • Model mapping  — "haiku" in name → light OpenAI model; everything else
                     (sonnet/opus) → strong OpenAI model. Defaults live in
                     _DEFAULT_STRONG/_DEFAULT_LIGHT; override with the
                     OPENAI_STRONG_MODEL / OPENAI_LIGHT_MODEL env vars.
  • stop_reason    — OpenAI "length" → Anthropic "max_tokens"
                     OpenAI "tool_calls" → Anthropic "tool_use"
                     everything else → "end_turn"

Ignored fields (silently dropped):
  • ``extra_headers`` — Anthropic prompt-caching headers have no OpenAI equivalent
  • ``cache_control`` blocks inside the system list — treated as plain text
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace


# ── Model mapping ──────────────────────────────────────────────────────────
# The adapter receives the Anthropic model string from model_config.get_model()
# and maps it to the closest OpenAI equivalent by tier:
#   sonnet/opus (writing-quality roles) → strong model
#   haiku (light routing roles)         → light model
# Override the defaults with OPENAI_STRONG_MODEL / OPENAI_LIGHT_MODEL when
# newer OpenAI models ship — no code change needed.

_DEFAULT_STRONG = "gpt-5.4"
_DEFAULT_LIGHT = "gpt-5.4-mini"

# GPT-5-family and o-series models reason before answering. For article prose
# a little thinking helps and a lot just burns latency and tokens, so default
# to "low"; override with OPENAI_REASONING_EFFORT (none/minimal/low/medium/high).
_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")

# Reasoning tokens count against max_completion_tokens. Callers size
# max_tokens for the VISIBLE output (Anthropic semantics), so give reasoning
# models headroom on top — otherwise a 1024-token brief call can come back
# empty because the budget was spent thinking.
_REASONING_HEADROOM_TOKENS = 4096


def _map_model(anthropic_model: str) -> str:
    """Map an Anthropic model name to the nearest OpenAI equivalent."""
    name = anthropic_model.lower()
    if "haiku" in name:
        return os.environ.get("OPENAI_LIGHT_MODEL", _DEFAULT_LIGHT)
    # sonnet, opus, or any unrecognised Claude model → strong tier
    return os.environ.get("OPENAI_STRONG_MODEL", _DEFAULT_STRONG)


def _completion_kwargs(model: str, max_tokens: int, *, with_tools: bool = False) -> dict:
    """Token budget + reasoning knobs appropriate for *model*.

    gpt-5.4 rejects reasoning_effort combined with function tools on
    /v1/chat/completions ("use /v1/responses instead"), so tool calls omit
    the knob and run at the model's default effort — structured extraction
    doesn't need tuned reasoning anyway. Text generations keep it.
    """
    if model.startswith(_REASONING_MODEL_PREFIXES):
        kwargs = {"max_completion_tokens": max_tokens + _REASONING_HEADROOM_TOKENS}
        if not with_tools:
            kwargs["reasoning_effort"] = os.environ.get("OPENAI_REASONING_EFFORT", "low")
        return kwargs
    return {"max_completion_tokens": max_tokens}


# ── Adapter classes ────────────────────────────────────────────────────────

class OpenAIAnthropicAdapter:
    """Top-level adapter: ``client.messages.create(...)`` just like Anthropic."""

    def __init__(self, openai_client) -> None:
        self.messages = _OpenAIMessages(openai_client)


class _OpenAIMessages:
    def __init__(self, openai_client) -> None:
        self._client = openai_client

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system=None,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        extra_headers: dict | None = None,    # ignored — Anthropic-only
        **_kwargs,
    ):
        oai_model = _map_model(model)
        oai_messages = _build_messages(system, messages)

        if tools:
            return await self._call_with_tools(
                oai_model, max_tokens, oai_messages, tools, tool_choice
            )
        return await self._call_text(oai_model, max_tokens, oai_messages)

    # ── tool-use path ──────────────────────────────────────────────────
    async def _call_with_tools(
        self, model, max_tokens, messages, tools, tool_choice
    ):
        oai_tools = [_convert_tool(t) for t in tools]
        oai_choice = _convert_tool_choice(tool_choice)

        response = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            tools=oai_tools,
            tool_choice=oai_choice,
            **_completion_kwargs(model, max_tokens, with_tools=True),
        )
        choice = response.choices[0]
        finish = choice.finish_reason

        content = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    parsed = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    logging.warning(
                        "OpenAI adapter: could not parse tool arguments for %s",
                        tc.function.name,
                    )
                    parsed = {}
                content.append(SimpleNamespace(
                    type="tool_use",
                    name=tc.function.name,
                    input=parsed,
                ))

        stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop_reason)

    # ── text path ──────────────────────────────────────────────────────
    async def _call_text(self, model, max_tokens, messages):
        response = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            **_completion_kwargs(model, max_tokens),
        )
        choice = response.choices[0]
        text = choice.message.content or ""
        # OpenAI uses "length" when the output was cut by max_tokens;
        # pipeline workers check for Anthropic's "max_tokens" string.
        stop_reason = "max_tokens" if choice.finish_reason == "length" else "end_turn"
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=stop_reason,
        )


# ── Conversion helpers ─────────────────────────────────────────────────────

def _extract_system_text(system) -> str:
    """Pull plain text out of Anthropic's system argument (string or block list)."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    # Cached block list: [{"type":"text","text":"...","cache_control":{...}}]
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(system)


def _build_messages(system, messages: list[dict]) -> list[dict]:
    """Build an OpenAI messages list from Anthropic system + user/assistant turns."""
    result = []
    sys_text = _extract_system_text(system)
    if sys_text:
        result.append({"role": "system", "content": sys_text})
    for msg in messages:
        result.append({"role": msg["role"], "content": msg["content"]})
    return result


def _convert_tool(tool: dict) -> dict:
    """Anthropic tool dict → OpenAI function tool dict."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        },
    }


def _convert_tool_choice(tool_choice: dict | None) -> str | dict:
    """Anthropic tool_choice → OpenAI tool_choice."""
    if not tool_choice:
        return "auto"
    if tool_choice.get("type") == "tool" and "name" in tool_choice:
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return "auto"
