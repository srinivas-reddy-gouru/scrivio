"""Local resource limits: request size and how much runs at once.

These keep one person's install from being taken down by one bad
request. They are process-local by design. Where Scrivio is hosted for
several people the limits have to be per user and shared across worker
processes, which a counter in one process's memory cannot be, and that
is a separate piece of work.
"""
from __future__ import annotations

import json
import threading

DEFAULT_BODY_BYTES = 1_000_000            # JSON requests: text, answers, settings
UPLOAD_BODY_BYTES = 8_000_000             # a resume file, base64 encoded
AUDIO_BODY_BYTES = 15_000_000             # a spoken answer, base64 encoded
PROVIDER_SECONDS = 180.0                  # one model call, end to end

_BODY_LIMITS = (
    ("/transcribe", AUDIO_BODY_BYTES),
    ("/resumes", UPLOAD_BODY_BYTES),
    ("/job-profiles", UPLOAD_BODY_BYTES),
)


def limit_for(path: str) -> int:
    for prefix, limit in _BODY_LIMITS:
        if path == prefix or path.startswith(prefix + "/"):
            return limit
    return DEFAULT_BODY_BYTES


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Refuses a request body that is too large, by its declared length
    when there is one and by counting as it arrives when there is not.

    The counting matters. A chunked upload declares no length, so a check
    that only reads the header lets it through, and the framework then
    buffers the whole thing into memory before any handler sees it."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        limit = limit_for(scope.get("path", ""))
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        declared = headers.get(b"content-length", b"").decode("latin-1")
        if declared.isdigit() and int(declared) > limit:
            await self._refuse(send, limit)
            return

        received = 0
        started = False

        async def counted():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        async def tracked(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except _TooLarge:
            if not started:
                await self._refuse(send, limit)

    @staticmethod
    async def _refuse(send, limit: int) -> None:
        body = json.dumps({
            "detail": f"That request is too large. The limit here is "
                      f"{limit // 1_000_000 or 1} MB."
        }).encode("utf-8")
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"connection", b"close")],
        })
        await send({"type": "http.response.body", "body": body})


# ── Admission ───────────────────────────────────────────────────────────────

class Busy(Exception):
    """Everything this gate allows is already running."""

    retry_after = 30


class _Slot:
    def __init__(self, gate: "Gate") -> None:
        self._gate, self._held = gate, True

    def leave(self) -> None:
        if self._held:                       # leaving twice frees one slot
            self._held = False
            self._gate._release()


class Gate:
    """At most `limit` of one kind of work at a time. Work beyond that is
    refused, not queued: a queue nobody bounded is the same problem with
    a delay, and each of these is a run of paid model calls."""

    def __init__(self, what: str, limit: int) -> None:
        self.what, self.limit = what, limit
        self.running = 0
        self._lock = threading.Lock()

    def enter(self) -> _Slot:
        with self._lock:
            if self.running >= self.limit:
                raise Busy(
                    f"{self.limit} {self.what} job(s) are already running, "
                    "which is the most this install runs at once. Wait for "
                    "one to finish, or cancel it, then try again.")
            self.running += 1
        return _Slot(self)

    def _release(self) -> None:
        with self._lock:
            self.running = max(0, self.running - 1)
