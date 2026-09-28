"""Outbound fetch policy (review item R07).

A job-description URL is typed by a user and a search-result URL is
chosen by the open web. Either can name an address inside the network
the server sits on, and the server would fetch it and hand the contents
to a model.

Networking is simulated throughout. The only real socket is a throwaway
listener these tests start themselves on loopback, which exists to prove
that it is never contacted. Nothing here probes a metadata endpoint or
any service the tests did not create.
"""
import asyncio
import gzip
import http.server
import threading

import httpx
import pytest

from pipeline import net_guard
from pipeline.net_guard import BlockedFetch, guarded_get
from pipeline.workers import extraction_worker
from pipeline.workers.extraction_worker import FetchError, fetch_with_retry

PUBLIC = "93.184.216.34"


def run(coro):
    return asyncio.run(coro)


def resolver(table: dict[str, list[str]]):
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        if host not in table:
            raise OSError(f"no such host {host}")
        return table[host]

    resolve.calls = calls
    return resolve


class Web:
    """A fake internet: records every request that actually went out."""

    def __init__(self, routes: dict):
        self.routes, self.seen = routes, []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        self.seen.append((request.url.host, host, request.url.path))
        reply = self.routes.get((host, request.url.path))
        if reply is None:
            return httpx.Response(404, text="not found")
        return reply(request) if callable(reply) else reply


def page(text="<html><body>A job description.</body></html>", **kwargs):
    return httpx.Response(200, text=text, headers={"content-type": "text/html"}, **kwargs)


# ── The reproduced case, against a real loopback listener ────────────

@pytest.fixture
def loopback_listener(monkeypatch):
    """A real listener on loopback. Its port is added to the allowed ports
    for the test, because otherwise the PORT rule would refuse the URL and
    the ADDRESS rule, which is the one under test, would never run."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"<html><body>internal admin console</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    monkeypatch.setattr(
        net_guard, "ALLOWED_PORTS", net_guard.ALLOWED_PORTS | {httpd.server_address[1]})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", hits
    httpd.shutdown()


def test_a_loopback_url_is_refused_and_never_contacted(loopback_listener, monkeypatch):
    base, hits = loopback_listener
    monkeypatch.delenv("USE_JINA_READER", raising=False)

    with pytest.raises(FetchError, match="not on the public internet"):
        run(fetch_with_retry(f"{base}/jd", max_attempts=1))

    assert hits == [], "the internal listener received a request"


def test_the_fallback_reader_is_not_a_way_around_the_policy(loopback_listener, monkeypatch):
    """Handing a refused URL to a third-party reader would leak it and,
    from inside a network, might still fetch it."""
    base, hits = loopback_listener
    asked = []

    async def jina_spy(url):
        asked.append(url)
        return "fetched by the fallback"

    monkeypatch.setattr(extraction_worker, "fetch_page_jina", jina_spy)

    with pytest.raises(FetchError):
        run(fetch_with_retry(f"{base}/jd", max_attempts=1))

    assert asked == [] and hits == []


# ── Addresses ────────────────────────────────────────────────────────

@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.8.9.10", "0.0.0.0", "10.0.0.5", "172.16.4.4", "192.168.1.1",
    "169.254.169.254",                 # link-local, where cloud metadata lives
    "100.64.0.1",                      # carrier-grade NAT
    "192.0.2.10", "198.18.0.1",        # documentation, benchmarking
    "224.0.0.1", "240.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",   # IPv4-mapped
    "::127.0.0.1",                     # IPv4-compatible
    "2002:7f00:0001::",                # 6to4 wrapping 127.0.0.1
    "64:ff9b::7f00:1",                 # NAT64 wrapping 127.0.0.1
    "2001:0000:4136:e378:8000:63bf:3fff:fdd2",   # Teredo
])
def test_non_public_addresses_are_refused(address):
    web = Web({})
    url = f"http://[{address}]/" if ":" in address else f"http://{address}/"

    with pytest.raises(BlockedFetch):
        run(guarded_get(url, transport=web.transport,
                        resolver=resolver({address: [address]})))

    assert web.seen == []


@pytest.mark.parametrize("spelling", [
    "2130706433", "0x7f000001", "0177.0.0.1", "127.1", "0x7f.0.0.1"])
def test_unusual_spellings_of_loopback_are_refused(spelling):
    """Judged by what the name RESOLVES to, so no spelling gets past a
    check that only recognised the ordinary one."""
    web = Web({})

    with pytest.raises(BlockedFetch):
        run(guarded_get(f"http://{spelling}/", transport=web.transport,
                        resolver=resolver({spelling: ["127.0.0.1"]})))

    assert web.seen == []


def test_a_name_that_resolves_inside_the_network_is_refused():
    web = Web({})

    with pytest.raises(BlockedFetch):
        run(guarded_get("https://jobs.example/posting", transport=web.transport,
                        resolver=resolver({"jobs.example": ["10.1.2.3"]})))

    assert web.seen == []


def test_one_private_answer_among_public_ones_refuses_the_name():
    """A resolver that returns both is offering a choice the attacker
    controls. There is no safe address to pick from that list."""
    web = Web({})

    with pytest.raises(BlockedFetch):
        run(guarded_get("https://jobs.example/", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC, "192.168.0.10"]})))

    assert web.seen == []


# ── URL shape ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://jobs.example/x", "gopher://jobs.example/",
    "javascript:alert(1)", "data:text/html,hi", "//jobs.example/", "jobs.example",
    "http://", "https://user:secret@jobs.example/", "https://token@jobs.example/",
    "https://jobs.example:22/", "https://jobs.example:6379/", "http://jobs.example:25/",
])
def test_malformed_and_out_of_policy_urls_are_refused(url):
    web = Web({})

    with pytest.raises(BlockedFetch):
        run(guarded_get(url, transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC]})))

    assert web.seen == []


# ── Rebinding and redirects ──────────────────────────────────────────

def test_the_connection_goes_to_the_address_that_was_checked():
    """DNS rebinding: answer with a public address when checked, a private
    one when connected. There is no second lookup to lie to. The request
    is addressed to the checked IP, and the name travels in the Host
    header and the TLS server name."""
    answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])

    async def rebinding(host, port):
        rebinding.calls += 1
        return next(answers)
    rebinding.calls = 0
    web = Web({("jobs.example", "/posting"): page()})

    body = run(guarded_get("https://jobs.example/posting",
                           transport=web.transport, resolver=rebinding))

    assert "A job description." in body.text
    assert rebinding.calls == 1
    assert web.seen == [(PUBLIC, "jobs.example", "/posting")]


def test_a_redirect_from_public_to_private_is_refused():
    web = Web({("jobs.example", "/posting"): httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/"})})

    with pytest.raises(BlockedFetch):
        run(guarded_get("https://jobs.example/posting", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC],
                                           "169.254.169.254": ["169.254.169.254"]})))

    assert web.seen == [(PUBLIC, "jobs.example", "/posting")], \
        "only the public hop may have been contacted"


def test_a_redirect_to_a_name_that_resolves_privately_is_refused():
    web = Web({("jobs.example", "/posting"): httpx.Response(
        301, headers={"location": "https://internal.example/admin"})})

    with pytest.raises(BlockedFetch):
        run(guarded_get("https://jobs.example/posting", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC],
                                           "internal.example": ["10.0.0.7"]})))

    assert [seen[1] for seen in web.seen] == ["jobs.example"]


def test_redirects_between_public_pages_are_followed_and_relative_ones_resolve():
    web = Web({
        ("jobs.example", "/old"): httpx.Response(302, headers={"location": "/new"}),
        ("jobs.example", "/new"): httpx.Response(
            302, headers={"location": "https://careers.example/role"}),
        ("careers.example", "/role"): page("<html><body>The role.</body></html>"),
    })

    body = run(guarded_get("https://jobs.example/old", transport=web.transport,
                           resolver=resolver({"jobs.example": [PUBLIC],
                                              "careers.example": ["93.184.216.35"]})))

    assert "The role." in body.text
    assert body.url == "https://careers.example/role"


def test_a_redirect_loop_ends():
    web = Web({("jobs.example", "/a"): httpx.Response(302, headers={"location": "/a"})})

    with pytest.raises(BlockedFetch, match="redirect"):
        run(guarded_get("https://jobs.example/a", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC]})))

    assert len(web.seen) <= net_guard.MAX_REDIRECTS + 1


def test_credentials_are_not_carried_to_another_host():
    seen_auth = []

    def second(request):
        seen_auth.append(request.headers.get("authorization"))
        return page()
    web = Web({
        ("reader.example", "/x"): httpx.Response(
            302, headers={"location": "https://elsewhere.example/y"}),
        ("elsewhere.example", "/y"): second,
    })

    run(guarded_get("https://reader.example/x", transport=web.transport,
                    headers={"Authorization": "Bearer reader-key"},
                    resolver=resolver({"reader.example": [PUBLIC],
                                       "elsewhere.example": ["93.184.216.35"]})))

    assert seen_auth == [None]


# ── Size, time, and type ─────────────────────────────────────────────

def test_a_public_page_is_fetched():
    web = Web({("jobs.example", "/posting"): page()})

    body = run(guarded_get("https://jobs.example/posting", transport=web.transport,
                           resolver=resolver({"jobs.example": [PUBLIC]})))

    assert body.status_code == 200 and "A job description." in body.text


def test_a_response_larger_than_the_limit_is_cut_off(monkeypatch):
    monkeypatch.setattr(net_guard, "MAX_BYTES", 10_000)
    web = Web({("jobs.example", "/big"): page("x" * 500_000)})

    with pytest.raises(BlockedFetch, match="larger"):
        run(guarded_get("https://jobs.example/big", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC]})))


def test_a_compressed_response_is_measured_by_what_it_expands_to(monkeypatch):
    """Two megabytes of zeros is a two-kilobyte download. The limit is on
    what would be held in memory, not on what crossed the wire."""
    monkeypatch.setattr(net_guard, "MAX_BYTES", 100_000)
    bomb = gzip.compress(b"0" * 2_000_000)
    assert len(bomb) < 5_000
    web = Web({("jobs.example", "/bomb"): httpx.Response(
        200, content=bomb,
        headers={"content-type": "text/html", "content-encoding": "gzip"})})

    with pytest.raises(BlockedFetch, match="larger"):
        run(guarded_get("https://jobs.example/bomb", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC]})))


def test_a_declared_length_over_the_limit_is_refused_before_reading(monkeypatch):
    monkeypatch.setattr(net_guard, "MAX_BYTES", 1_000)
    web = Web({("jobs.example", "/big"): httpx.Response(
        200, content=b"x" * 5_000, headers={"content-type": "text/html"})})

    with pytest.raises(BlockedFetch, match="larger"):
        run(guarded_get("https://jobs.example/big", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC]})))


@pytest.mark.parametrize("content_type", [
    "application/octet-stream", "application/pdf", "image/png", "video/mp4",
    "application/zip", "application/x-msdownload"])
def test_content_that_is_not_a_page_is_refused(content_type):
    web = Web({("jobs.example", "/file"): httpx.Response(
        200, content=b"\x00\x01", headers={"content-type": content_type})})

    with pytest.raises(BlockedFetch, match="content type"):
        run(guarded_get("https://jobs.example/file", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC]})))


def test_a_response_that_never_finishes_is_abandoned(monkeypatch):
    monkeypatch.setattr(net_guard, "TOTAL_SECONDS", 0.3)

    async def stalled(host, port):
        await asyncio.sleep(5)
        return [PUBLIC]

    started = asyncio.new_event_loop().time()
    with pytest.raises(BlockedFetch, match="too long"):
        run(guarded_get("https://jobs.example/", transport=Web({}).transport,
                        resolver=stalled))
    assert asyncio.new_event_loop().time() - started < 2


def test_an_http_error_is_reported_with_its_status():
    web = Web({})

    with pytest.raises(net_guard.FetchFailed) as caught:
        run(guarded_get("https://jobs.example/missing", transport=web.transport,
                        resolver=resolver({"jobs.example": [PUBLIC]})))

    assert caught.value.status_code == 404


# ── Through the API ──────────────────────────────────────────────────

@pytest.mark.parametrize("route, body", [
    ("/resumes", {"resume_text": "Sam Okafor\nSoftware Engineer\n" * 20}),
    ("/job-profiles", {"role_title": "Engineer", "resume_text": "Sam Okafor " * 40}),
])
def test_both_studios_refuse_an_internal_posting_url(loopback_listener, route, body):
    from fastapi.testclient import TestClient
    from api import server

    base, hits = loopback_listener

    r = TestClient(server.app).post(route, json={**body, "jd_url": f"{base}/posting"})

    assert r.status_code == 422
    assert "not on the public internet" in r.json()["detail"]
    assert "internal admin console" not in r.text
    assert hits == []
