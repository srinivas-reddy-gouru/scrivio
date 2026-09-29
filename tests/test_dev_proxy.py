"""The dev server reaches every API route (review item R16).

Vite proxied three paths. Every other request the interface makes in
development was answered by Vite itself, with index.html and a 200. This
test reads the routes the application declares and the routes the proxy
forwards, and fails when they differ, so the list cannot go stale again.
"""
import re
from pathlib import Path

from fastapi.routing import APIRoute

from api import server

CONFIG = Path(__file__).resolve().parents[1] / "web" / "vite.config.ts"


def proxied() -> set[str]:
    text = CONFIG.read_text()
    block = re.search(r"API_ROUTES\s*=\s*\[(.*?)\]", text, re.S).group(1)
    return set(re.findall(r'"(/[a-z-]+)"', block))


def declared() -> set[str]:
    return {"/" + route.path.split("/")[1]
            for route in server.app.router.routes if isinstance(route, APIRoute)}


def test_every_api_route_is_proxied_in_development():
    missing = declared() - proxied() - {"/classic"}       # /classic is a page

    assert missing == set(), f"the dev proxy does not forward: {sorted(missing)}"


def test_the_proxy_forwards_nothing_the_api_does_not_answer():
    assert proxied() - declared() == set()


def test_the_interface_only_calls_routes_that_are_proxied():
    """From the other side: every path the client code requests."""
    source = Path(__file__).resolve().parents[1] / "web" / "src"
    called = set()
    for path in [*source.rglob("*.ts"), *source.rglob("*.tsx")]:
        for match in re.finditer(r'[`"](/[a-z][a-z-]*)(?:[/?`"$])', path.read_text()):
            called.add(match.group(1))
    api_like = {c for c in called if c in declared() | proxied()}

    assert api_like, "found no API calls at all: the scan is broken"
    assert api_like - proxied() == set()
