"""The real server, with model calls that take a long time.

Started by the restart tests so that there is work genuinely in flight
when the process is killed. Everything about the application is the
real thing; only the three functions that would call a model are
replaced, with ones that report a little progress and then wait.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

from api import server  # noqa: E402
from pipeline.schemas.models import ProgressEvent  # noqa: E402


async def slow_article(request, *, progress_callback=None):
    for stage in ("brief", "search"):
        await progress_callback(ProgressEvent(
            type="stage_started", stage=stage, message=f"working on {stage}"))
        await progress_callback(ProgressEvent(type="stage_completed", stage=stage))
    await asyncio.sleep(600)


async def slow_model_call(*args, **kwargs):
    await asyncio.sleep(600)


class _Client:
    pass


server.generate_article = slow_article
server._require_providers = lambda request: None
server._maybe_request_clarification = lambda request: asyncio.sleep(0)
server._client_for_session = lambda *a, **k: (_Client(), "balanced")
server.extract_resume = slow_model_call
server.review_resume = slow_model_call
server.tailor_resume = slow_model_call

if __name__ == "__main__":
    uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ["PORT"]),
                log_level="warning")
