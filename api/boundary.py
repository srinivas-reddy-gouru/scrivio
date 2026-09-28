"""The local application boundary.

Scrivio holds resumes, interview transcripts, and provider credentials,
and until this module existed its API answered anyone who could reach the
port: list, read, export, delete, change settings, start paid work.

This is the boundary for ONE person running Scrivio on their own machine.
It is three independent checks, because each stops a different caller:

  Host      A request must be addressed to a name this server answers
            to. Stops DNS rebinding, where a hostile page points its own
            domain at 127.0.0.1 and then talks to us "same-origin".
  Origin    A browser request from another site is refused, and unsafe
            methods must not come from a cross-site context. Stops a
            page you happen to have open from driving the API.
  Session   Every API route needs a session cookie, obtained by typing
            the pairing code the server printed in its own terminal.
            Stops everything that is not the person at the keyboard.

CORS is not on that list. CORS tells a browser what it may READ; it
authenticates nobody and does nothing about a caller that is not a
browser.

What this is not: multi-user isolation. There is one session secret and
one namespace of data. Hosting this for several people needs identities
and ownership on every record, which is a separate piece of work.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from starlette.routing import Match, Mount

COOKIE_NAME = "scrivio_session"
SESSION_DAYS = 30
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# Reachable without a session. Everything else that is a route needs one;
# the default is closed, so a route added tomorrow is protected tomorrow.
PUBLIC_PATHS = frozenset({
    "/health", "/auth/status", "/auth/pair", "/auth/logout",
})

# The interface renders text a model wrote after reading the open web.
# It is sanitised before it reaches the page; this is the line behind
# that one. With no 'unsafe-inline' in script-src, an event handler or a
# <script> that slipped through is inert, and connect-src keeps a page
# from sending what it can see anywhere but here.
#   style-src 'unsafe-inline'  diagrams are SVG with their own <style>
#   img-src http(s)            articles cite figures on other sites
#   media-src blob:            the interviewer's voice is played from one
MODERN_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https: http:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
# The older single-file interface is built on inline scripts and CDN
# libraries, so its policy cannot forbid inline script and does not
# pretend to. It still cannot be framed, cannot post forms elsewhere, and
# cannot open connections anywhere but this server.
CLASSIC_CSP = "; ".join([
    "default-src 'self' 'unsafe-inline' data: blob: https://cdn.tailwindcss.com "
    "https://cdn.jsdelivr.net https://fonts.googleapis.com https://fonts.gstatic.com",
    "img-src 'self' data: blob: https: http:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
# Set by the server once it knows which interface it is serving at "/".
modern_interface_at_root = True
_NO_POLICY = ("/docs", "/redoc")     # FastAPI's own pages, behind the session


def content_policy(path: str) -> str | None:
    if path.startswith(_NO_POLICY):
        return None
    if path.startswith("/classic") or not modern_interface_at_root:
        return CLASSIC_CSP
    return MODERN_CSP


_PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
_MAX_FAILED_PAIRINGS = 5
_PAIRING_LOCKOUT_SECONDS = 60


def state_dir() -> Path:
    configured = os.environ.get("SCRIVIO_STATE_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / ".scrivio"


# ── Session secret ──────────────────────────────────────────────────────────

def _secret_path() -> Path:
    return state_dir() / "session.key"


def _load_secret(create: bool = True) -> bytes | None:
    """The key that signs sessions. Kept on disk so a restart does not sign
    everyone out, and readable only by the account that runs the server."""
    path = _secret_path()
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except FileNotFoundError:
        pass
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass
    secret = secrets.token_bytes(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    return secret


def forget_every_browser() -> None:
    """Replace the key. Every session ever issued stops verifying."""
    try:
        _secret_path().unlink()
    except FileNotFoundError:
        pass
    _load_secret(create=True)


def _sign(payload: str, secret: bytes) -> str:
    digest = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def mint_session(now: float | None = None) -> str:
    secret = _load_secret()
    issued = int(now if now is not None else time.time())
    payload = f"v1.{issued}.{secrets.token_urlsafe(12)}"
    return f"{payload}.{_sign(payload, secret)}"


def session_is_valid(value: str, now: float | None = None) -> bool:
    secret = _load_secret(create=False)
    if not secret or not value or value.count(".") != 3:
        return False
    payload, _, signature = value.rpartition(".")
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return False
    try:
        version, issued, _nonce = payload.split(".")
        age = (now if now is not None else time.time()) - int(issued)
    except ValueError:
        return False
    return version == "v1" and -300 <= age <= SESSION_DAYS * 86400


# ── Pairing ─────────────────────────────────────────────────────────────────

class Pairing:
    """A short code shown in the server's own terminal. Whoever can read
    that terminal is, for a local tool, the owner. The code goes in a
    request body, never a URL, so it is not written to an access log or
    left in browser history."""

    def __init__(self) -> None:
        self._code = ""
        self._failures = 0
        self._locked_until = 0.0

    @property
    def code(self) -> str:
        if not self._code:
            raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(8))
            self._code = f"{raw[:4]}-{raw[4:]}"
        return self._code

    def locked_for(self, now: float | None = None) -> int:
        return max(0, int(self._locked_until - (now or time.time()) + 0.999))

    def attempt(self, offered: str, now: float | None = None) -> bool:
        now = now or time.time()
        if now < self._locked_until:
            return False
        normalised = "".join(ch for ch in (offered or "").upper() if ch.isalnum())
        expected = self.code.replace("-", "")
        if normalised and hmac.compare_digest(normalised, expected):
            self._failures = 0
            self._code = ""          # single use: the next browser gets a new one
            return True
        self._failures += 1
        if self._failures >= _MAX_FAILED_PAIRINGS:
            self._failures = 0
            self._locked_until = now + _PAIRING_LOCKOUT_SECONDS
        return False


pairing = Pairing()


def announce_pairing_code(stream=None) -> None:
    """Printed, not logged: log handlers ship text to files and services,
    and this is the one line that should exist only on the screen."""
    stream = stream or sys.stderr
    print(
        "\n  Scrivio needs to know this browser is yours.\n"
        f"  When it asks for a pairing code, enter:  {pairing.code}\n"
        "  (A new code is issued each time one is used or the server restarts.)\n",
        file=stream, flush=True,
    )


# ── Host and origin policy ──────────────────────────────────────────────────

def _configured(name: str) -> list[str]:
    return [v.strip().lower() for v in os.environ.get(name, "").split(",") if v.strip()]


def _hostname(value: str) -> str:
    """'127.0.0.1:8899' -> '127.0.0.1', '[::1]:8899' -> '::1'."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def host_is_allowed(host_header: str) -> bool:
    name = _hostname(host_header)
    if not name:
        return False
    if name in _LOOPBACK_HOSTS or name.endswith(".localhost"):
        return True
    return name in _configured("SCRIVIO_ALLOWED_HOSTS")


def origin_is_allowed(origin: str) -> bool:
    if origin == "null":
        return False                     # sandboxed frames, file:// pages
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if origin.rstrip("/").lower() in _configured("SCRIVIO_ALLOWED_ORIGINS"):
        return True
    return host_is_allowed(parts.hostname)


def request_is_authenticated(cookies: dict[str, str]) -> bool:
    return session_is_valid(cookies.get(COOKIE_NAME, ""))


# ── The middleware ──────────────────────────────────────────────────────────

_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _parse_cookies(header: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            jar[name] = value
    return jar


class LocalBoundary:
    """Pure ASGI, not BaseHTTPMiddleware: progress streams must pass
    through untouched, and the wrapper variety buffers and re-frames."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        if not host_is_allowed(headers.get("host", "")):
            await self._refuse(scope, send, 400, "This server does not answer to that host name.")
            return

        origin = headers.get("origin")
        if origin is not None and not origin_is_allowed(origin):
            await self._refuse(scope, send, 403, "Requests from that origin are not accepted.")
            return
        if method in _UNSAFE and origin is None:
            # No Origin header: either not a browser, or a browser that
            # did say where the request came from another way.
            fetch_site = headers.get("sec-fetch-site")
            if fetch_site not in (None, "same-origin", "none"):
                await self._refuse(scope, send, 403, "Cross-site requests are not accepted.")
                return

        # A preflight carries no credentials and runs no route: it only
        # asks what would be allowed. Refusing it would break the one
        # cross-origin setup that was deliberately configured.
        preflight = method == "OPTIONS"
        if not preflight and self._is_protected(scope, path) and not request_is_authenticated(
                _parse_cookies(headers.get("cookie", ""))):
            await self._refuse(
                scope, send, 401,
                "This browser is not paired with Scrivio yet. Enter the "
                "pairing code shown in the terminal where the server is running.")
            return

        await self.app(scope, receive, self._hardened(send, path))

    def _is_protected(self, scope, path: str) -> bool:
        if path in PUBLIC_PATHS:
            return False
        if scope["type"] == "websocket":
            return True
        router = scope.get("app")
        routes = getattr(getattr(router, "router", None), "routes", None) or []
        for route in routes:
            if isinstance(route, Mount):
                continue            # the interface bundle: no data, no secrets
            match, _ = route.matches(scope)
            if match != Match.NONE:
                return True
        return False

    @staticmethod
    def _hardened(send, path: str = ""):
        policy = content_policy(path)

        async def wrapped(message) -> None:
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                ]
                if policy:
                    extra.append((b"content-security-policy", policy.encode("ascii")))
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [
                    (k, v) for k, v in extra if k not in names]
            await send(message)
        return wrapped

    @staticmethod
    async def _refuse(scope, send, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start", "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def session_cookie_header(value: str, *, clear: bool = False) -> str:
    """HttpOnly keeps it from scripts, SameSite=Strict keeps it off
    cross-site requests. Not marked Secure: this is served over plain
    HTTP on loopback, where a Secure cookie would never be sent back."""
    if clear:
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    return (f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_DAYS * 86400}")
