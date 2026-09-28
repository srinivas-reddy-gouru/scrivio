"""Fact-checking on Anthropic, for an install that has no OpenAI key.

Claim verification and search-query generation were written against the
OpenAI SDK. With only an Anthropic key configured there was nothing to
run them on, and they ran on the mock: the article was written by a real
model and "verified" by a function that approves everything.

This is the same narrow surface the command-line facade implements,
over the Anthropic SDK:

    chat.completions.create(model, messages)       -> .choices[0].message.content
    beta.chat.completions.parse(model, messages,
                                response_format=M)  -> .choices[0].message.parsed
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.model_config import get_model


def _tier(model: str) -> str:
    """A small-model request stays on the small tier. The role names are
    the ones the settings screen already lets the user assign a model to."""
    small = "mini" in (model or "") or "nano" in (model or "")
    return get_model("relevance" if small else "editor", "balanced")


def _split(messages: list[dict]) -> tuple[str, list[dict]]:
    system = "\n".join(str(m["content"]) for m in messages if m.get("role") == "system")
    turns = [
        {"role": "assistant" if m.get("role") == "assistant" else "user",
         "content": str(m["content"])}
        for m in messages if m.get("role") != "system"
    ]
    return system, turns or [{"role": "user", "content": ""}]


def _reply(**message) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(**message), finish_reason="stop")])


class AnthropicOpenAIFacade:
    def __init__(self, client=None) -> None:
        if client is None:
            from pipeline.providers.clients import anthropic_client
            client = anthropic_client()
        self._client = client
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.beta = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(parse=self._parse)))

    async def _create(self, model: str = "", messages: list | None = None, **kwargs):
        system, turns = _split(messages or [])
        response = await self._client.messages.create(
            model=_tier(model), system=system, messages=turns,
            max_tokens=int(kwargs.get("max_tokens") or 2048),
        )
        text = "".join(
            block.text for block in response.content
            if getattr(block, "type", "") == "text")
        return _reply(content=text)

    async def _parse(self, model: str = "", messages: list | None = None,
                     response_format=None, **kwargs):
        system, turns = _split(messages or [])
        name = getattr(response_format, "__name__", "structured_output")
        response = await self._client.messages.create(
            model=_tier(model), system=system, messages=turns,
            max_tokens=int(kwargs.get("max_tokens") or 4096),
            tools=[{
                "name": name,
                "description": "Return the result in exactly this structure.",
                "input_schema": response_format.model_json_schema(),
            }],
            tool_choice={"type": "tool", "name": name},
        )
        block = next(b for b in response.content if getattr(b, "type", "") == "tool_use")
        return _reply(parsed=response_format.model_validate(block.input))
