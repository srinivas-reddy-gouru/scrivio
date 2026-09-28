"""The provider SDK clients, built one way.

Left to their defaults the SDKs wait ten minutes for a response. A
provider that has stalled would hold a job, and the slot it occupies,
for that long. Retries are the SDKs' own, and bounded: each retry of a
generation call is another paid call, so the number is small and fixed
rather than something a loop elsewhere can multiply.
"""
from __future__ import annotations

import httpx

from pipeline.runtime_mode import refuse_in_demo

PROVIDER_SECONDS = 180.0
PROVIDER_CONNECT_SECONDS = 10.0
PROVIDER_RETRIES = 2


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(PROVIDER_SECONDS, connect=PROVIDER_CONNECT_SECONDS)


def anthropic_client():
    refuse_in_demo("build a client for Anthropic")
    import anthropic
    return anthropic.AsyncAnthropic(timeout=_timeout(), max_retries=PROVIDER_RETRIES)


def openai_client():
    refuse_in_demo("build a client for OpenAI")
    import openai
    return openai.AsyncOpenAI(timeout=_timeout(), max_retries=PROVIDER_RETRIES)
