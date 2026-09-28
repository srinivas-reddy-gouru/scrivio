"""The policy for fetching a URL that came from outside.

Two kinds of URL reach this program from outside it: one a user typed as
a job posting, and ones a search engine returned. The program fetches
them from wherever it is running, which may be a laptop on an office
network or a machine in a cloud, and hands what it got to a model. So a
URL is a way to ask this machine to read something only this machine can
reach: another service on loopback, a router's admin page, a cloud
metadata endpoint that returns credentials.

The rules, each enforced before any byte is sent:

  shape      http or https, a host, no credentials, an ordinary web port
  address    every address the name resolves to must be public; one
             private answer among public ones refuses the whole name
  pinning    the connection is made to the address that was checked.
             The name is resolved ONCE per hop. Checking a name and then
             letting the HTTP client look it up again is two lookups, and
             DNS rebinding is answering them differently.
  redirects  followed by hand, so each hop is put through all of the
             above; credentials are not carried to a different host
  response   a page, not a file; bounded in bytes AS DECOMPRESSED and
             in total time

This is the application's half. Where Scrivio is hosted for other
people, outbound traffic should ALSO be restricted by the network it runs
in. A check in code is one mistake away from open; an egress rule is not.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
TOTAL_SECONDS = 25.0
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
ALLOWED_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml", "text/plain", "text/markdown",
    "application/xml", "text/xml", "application/json",
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NOT_FORWARDED = ("authorization", "cookie", "proxy-authorization")


class FetchFailed(Exception):
    """The fetch was allowed and did not succeed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BlockedFetch(FetchFailed):
    """The fetch was refused by policy. Trying again, or trying another
    way, is not a remedy: the answer is the same by design."""


@dataclass(frozen=True)
class Fetched:
    url: str            # where the content finally came from
    status_code: int
    content_type: str
    text: str


# ── Addresses ───────────────────────────────────────────────────────────────

def is_public_address(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is one the public internet routes to.

    IPv6 has several ways to carry an IPv4 address inside it, and a check
    that only looks at the outside calls ::ffff:127.0.0.1 a perfectly
    good global address. Each wrapper is opened and the address inside is
    judged."""
    try:
        ip = ipaddress.ip_address(address) if isinstance(address, str) else address
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return is_public_address(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_address(ip.sixtofour)
        if ip.teredo is not None:
            return all(is_public_address(part) for part in ip.teredo)
        if ip in _NAT64 or int(ip) < 2 ** 32:          # NAT64, IPv4-compatible
            return is_public_address(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return bool(
        ip.is_global
        and not ip.is_multicast and not ip.is_reserved
        and not ip.is_loopback and not ip.is_link_local
        and not ip.is_private and not ip.is_unspecified
    )


async def _system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def resolve_public(host: str, port: int, resolver=None) -> str:
    """One lookup. Every answer must be public; the first is returned and
    is the address the connection will be made to."""
    try:
        addresses = await (resolver or _system_resolver)(host, port)
    except OSError as exc:
        raise FetchFailed(f"Could not resolve {host}") from exc
    addresses = [a.split("%", 1)[0] for a in addresses]      # drop any zone id
    if not addresses:
        raise FetchFailed(f"Could not resolve {host}")
    if not all(is_public_address(a) for a in addresses):
        raise BlockedFetch(
            f"{host} resolves to an address that is not on the public "
            "internet, so it was not fetched.")
    return addresses[0]


# ── URL shape ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str           # path and query, as they will be requested

    @property
    def host_header(self) -> str:
        default = 443 if self.scheme == "https" else 80
        name = f"[{self.host}]" if ":" in self.host else self.host
        return name if self.port == default else f"{name}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host_header}{self.path}"


def check_url(url: str) -> Target:
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError as exc:
        raise BlockedFetch("That is not a valid web address.") from exc
    if parts.scheme not in ("http", "https"):
        raise BlockedFetch("Only http and https addresses can be fetched.")
    if not parts.hostname:
        raise BlockedFetch("That address has no host name.")
    if parts.username is not None or parts.password is not None:
        raise BlockedFetch("An address with a user name or password in it is not fetched.")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedFetch(f"Port {port} is not one a web page is served on.")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        host = parts.hostname.lower()
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(parts.scheme, host, port, path)


# ── The fetch ───────────────────────────────────────────────────────────────

async def guarded_get(
    url: str, *, headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None, resolver=None,
    connect_timeout: float = 10.0, read_timeout: float = 15.0,
) -> Fetched:
    """GET a URL from outside, under the policy above, or raise.

    `transport` and `resolver` exist so tests can stand in for the
    network. In normal use both are left alone."""
    try:
        return await asyncio.wait_for(
            _fetch(url, dict(headers or {}), transport, resolver,
                   connect_timeout, read_timeout),
            timeout=TOTAL_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise BlockedFetch(
            f"Fetching that address took too long (over {TOTAL_SECONDS:g}s) "
            "and was abandoned.") from exc


async def _fetch(url, headers, transport, resolver, connect_timeout, read_timeout) -> Fetched:
    timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
    first_host = None
    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=False,
        trust_env=False,          # no proxy from the environment: it would
    ) as client:                  # make the connection somewhere unchecked
        for _hop in range(MAX_REDIRECTS + 1):
            target = check_url(url)
            address = await resolve_public(target.host, target.port, resolver)
            if first_host is None:
                first_host = target.host
            sent = {k: v for k, v in headers.items()
                    if target.host == first_host or k.lower() not in _NOT_FORWARDED}
            sent["Host"] = target.host_header
            literal = f"[{address}]" if ":" in address else address
            pinned = f"{target.scheme}://{literal}:{target.port}{target.path}"
            extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
            try:
                async with client.stream(
                    "GET", pinned, headers=sent, extensions=extensions,
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "")
                        if not location:
                            raise FetchFailed("Redirected without a destination.",
                                              response.status_code)
                        url = urljoin(target.url, location)
                        continue
                    return await _read(response, target.url)
            except httpx.TimeoutException as exc:
                raise FetchFailed(f"Timed out fetching {target.url}") from exc
            except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
                # A failed TLS handshake can surface as a bare ssl.SSLError
                # rather than an httpx one. Either way it is a failed
                # fetch, not a crash, and the message carries no detail
                # from the far end.
                raise FetchFailed(
                    f"Request error for {target.url}: {type(exc).__name__}") from exc
    raise BlockedFetch(f"Too many redirects (more than {MAX_REDIRECTS}).")


async def _read(response: httpx.Response, url: str) -> Fetched:
    if response.status_code != 200:
        raise FetchFailed(
            f"Fetch failed for {url} with status {response.status_code}",
            status_code=response.status_code)
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type not in ALLOWED_CONTENT_TYPES:
        raise BlockedFetch(
            f"That address returned content type {content_type}, which is "
            "not a page that can be read.")
    declared = response.headers.get("content-length", "")
    if (declared.isdigit() and int(declared) > MAX_BYTES
            and "content-encoding" not in response.headers):
        raise BlockedFetch(
            f"That page is larger than the {MAX_BYTES // 1_000_000} MB limit.")
    received = bytearray()
    async for chunk in response.aiter_bytes():     # decompressed as it arrives
        received.extend(chunk)
        if len(received) > MAX_BYTES:
            raise BlockedFetch(
                f"That page is larger than the {MAX_BYTES // 1_000_000} MB limit "
                "once decompressed.")
    encoding = response.charset_encoding or "utf-8"
    try:
        text = bytes(received).decode(encoding, errors="replace")
    except LookupError:
        text = bytes(received).decode("utf-8", errors="replace")
    return Fetched(url=url, status_code=200, content_type=content_type, text=text)
