"""Being able to tell what happened, without recording what was said.

Three things, all small:

  a request id   on every request and in every log line written while
                 handling it, returned in X-Request-ID, so a report of
                 "it failed" can be matched to the line that says why
  readiness      separate from liveness. "The process is up" and "it can
                 do useful work" are different answers, and only the
                 first was being given
  diagnostics    what is running, what stalled, how long stages took

None of it calls a provider: a health check that costs money is a bill
that arrives every ten seconds. None of it logs a request body, a
resume, an answer, or a key.
"""
from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
job_id: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="-")

STALL_SECONDS = 600           # a running job that has said nothing for this long
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class ContextFilter(logging.Filter):
    """Puts the current request and job on every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        record.job_id = job_id.get()
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s [req %(request_id)s job %(job_id)s] %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


class RequestId:
    """Gives every request an id. One sent by the caller is kept if it
    looks like an id, so a request can be followed across a proxy; it is
    never trusted as anything more than a label."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        offered = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id":
                offered = value.decode("latin-1")
        rid = offered if _ID_RE.match(offered) else uuid.uuid4().hex[:16]
        token = request_id.set(rid)

        async def tagged(message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", rid.encode("ascii"))]
            await send(message)

        try:
            await self.app(scope, receive, tagged)
        finally:
            request_id.reset(token)


# ── What a user is told when something fails ────────────────────────────────

def public_error(exc: BaseException, reference: str) -> str:
    """The message shown for a failure.

    An exception's text is written for whoever is debugging. It can hold
    a file path, a fragment of a prompt, or the text a validator
    rejected. Messages written for users are passed through; anything
    else becomes the kind of failure and a reference that finds the
    detail in the log."""
    from pipeline.runtime_mode import ProviderUnavailable

    if isinstance(exc, ProviderUnavailable):
        return str(exc)
    kind = type(exc).__name__
    if "Timeout" in kind or isinstance(exc, TimeoutError):
        what = "The model provider took too long to answer."
    elif "RateLimit" in kind:
        what = "The model provider is limiting requests right now."
    elif "Authentication" in kind or "PermissionDenied" in kind:
        what = "The model provider rejected the credentials. Check the key in Settings."
    elif "Connection" in kind:
        what = "The model provider could not be reached."
    else:
        what = "The run failed and was stopped."
    return f"{what} Nothing was lost. Reference: {reference} ({kind})."


# ── Stage timings ───────────────────────────────────────────────────────────

def stage_timings(events) -> list[dict]:
    """How long each stage took, from the events the job already records."""
    started: dict[str, float] = {}
    timings: list[dict] = []
    for _seq, event in events:
        at = event.timestamp.timestamp()
        if event.type == "stage_started":
            started[event.stage] = at
        elif event.type == "stage_completed" and event.stage in started:
            timings.append({
                "stage": event.stage,
                "seconds": round(at - started.pop(event.stage), 2),
                "cached": bool(event.data.get("cached")),
            })
    for stage, at in started.items():
        timings.append({"stage": stage, "seconds": None, "running_for":
                        round(time.time() - at, 1)})
    return timings
