"""The provider SDK clients, built one way.

Left to their defaults the SDKs wait ten minutes for a response. A
provider that has stalled would hold a job, and the slot it occupies,
for that long. Retries are the SDKs' own, and bounded: each retry of a
generation call is another paid call, so the number is small and fixed
rather than something a loop elsewhere can multiply.

What the limits are, exactly
----------------------------
PROVIDER_SECONDS is a limit on each wait inside one attempt: to connect,
to send, and between one piece of the response and the next. It is not
a limit on the attempt, and it was described as one. A response that
arrives a little at a time, each piece inside the limit, satisfies it
for as long as the pieces keep coming. Nor is it a limit across
retries: three attempts can each take the full time.

PROVIDER_DEADLINE_SECONDS is the limit on the call: one request for a
completion, from when it is made to when it returns, counting every
retry and however the response arrives. When it passes, the call is
abandoned and fails with ProviderTimedOut. Retries are budgeted inside
it. They are not added to it.
"""
from __future__ import annotations

import asyncio
import functools
import inspect

import httpx

from pipeline.runtime_mode import ProviderUnavailable, refuse_in_demo

PROVIDER_SECONDS = 180.0
PROVIDER_CONNECT_SECONDS = 10.0
PROVIDER_RETRIES = 2
PROVIDER_DEADLINE_SECONDS = 300.0
_STOP_SECONDS = 5.0                      # how long an abandoned call has to stop

_SDKS = ("anthropic", "openai")


class ProviderTimedOut(ProviderUnavailable):
    """The call took longer than the deadline and was abandoned. The
    message is shown to the user as it is."""


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(PROVIDER_SECONDS, connect=PROVIDER_CONNECT_SECONDS)


async def within_deadline(call, what: str, seconds: float | None = None):
    """Run `call`, and stop it if it has not finished by the deadline.

    The call runs as a task of its own and is waited on from outside.
    asyncio.wait_for() was tried first and did not end these calls: the
    HTTP library underneath has cancel scopes of its own, and between
    the two the cancellation came out as a cancellation and not as a
    timeout, so the caller was cancelled along with the call."""
    limit = PROVIDER_DEADLINE_SECONDS if seconds is None else seconds
    task = asyncio.ensure_future(call)
    try:
        done, _ = await asyncio.wait({task}, timeout=limit)
    except asyncio.CancelledError:
        task.cancel()                    # the caller was cancelled: so is the call
        await asyncio.wait({task}, timeout=_STOP_SECONDS)
        raise
    if done:
        return task.result()
    task.cancel()
    await asyncio.wait({task}, timeout=_STOP_SECONDS)
    if task.done() and not task.cancelled():
        task.exception()                 # looked at, so it is not reported as unretrieved
    raise ProviderTimedOut(
        f"{what} did not answer within {limit:g} seconds, counting retries, "
        "and the call was abandoned. Nothing was saved from it. Try again, "
        "or choose a faster model in Settings.")


class Deadlined:
    """An SDK client, with the deadline on every call made through it.

    It stands in front of the client and of the namespaces under it
    (`messages`, `chat.completions`, `audio.speech`), and passes
    everything else through untouched."""

    def __init__(self, target, what: str) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_what", what)

    def __getattr__(self, name: str):
        value = getattr(self._target, name)
        if inspect.isroutine(value):
            @functools.wraps(value)
            def call(*args, **kwargs):
                result = value(*args, **kwargs)
                if inspect.isawaitable(result):
                    return within_deadline(result, self._what)
                return result
            return call
        if type(value).__module__.split(".")[0] in _SDKS and not isinstance(value, type):
            return Deadlined(value, self._what)
        return value

    def __setattr__(self, name: str, value) -> None:
        setattr(self._target, name, value)

    def __repr__(self) -> str:
        return f"Deadlined({self._target!r})"


def anthropic_client():
    refuse_in_demo("build a client for Anthropic")
    import anthropic
    return Deadlined(
        anthropic.AsyncAnthropic(timeout=_timeout(), max_retries=PROVIDER_RETRIES),
        "Anthropic")


def openai_client():
    refuse_in_demo("build a client for OpenAI")
    import openai
    return Deadlined(
        openai.AsyncOpenAI(timeout=_timeout(), max_retries=PROVIDER_RETRIES),
        "OpenAI")
