"""A real server and a real browser, both on synthetic data.

These tests exist because a parser test can only say what the parser
emits. Whether anything RUNS is a question for a browser, asked through
the same code path a reader would take to open an article.

Nothing here touches the developer's resumes, articles, settings, or
provider keys: the server gets its own output directory, its own state
directory, and a settings file that does not exist.

Skipped, not failed, when there is no browser to drive or no built
interface to load. CI installs both (see .github/workflows).

The fixtures last for one test module, not the session. Playwright's
synchronous API runs an event loop on the test thread for as long as it
is open, and while it is, every later test that calls asyncio.run() fails
with "cannot be called from a running event loop". Held for the session,
that broke 238 unrelated tests on a clean install.
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "web" / "dist" / "index.html"

# ── What a failing browser test leaves behind ─────────────────────────
# One of these tests once timed out loading a page, and the only record
# was "Page.goto: Timeout 30000ms exceeded". That says a page was being
# waited for and nothing about what the page was waiting for. So every
# page opened here keeps an account of its requests, and when a test
# fails the account is written down, with the end of each server's log.
#
# This records. It does not retry, and it does not give anything longer
# to finish.
DIAGNOSTICS = Path(os.environ.get("SCRIVIO_TEST_DIAGNOSTICS")
                   or REPO / ".scrivio" / "test-diagnostics")
_OPEN: list = []                 # pages opened by the test that is running
_LOGS: dict[str, Path] = {}      # server logs, by what they are


SLOW = 5.0                       # seconds. Longer than this is written down


class timed:
    """How long a step of setting up or taking down took. One that takes
    longer than SLOW is written to the diagnostics folder with the time
    of day, so that a slow start or a slow stop can be set beside
    whatever else was going on. It changes nothing about the step."""

    def __init__(self, what: str) -> None:
        self.what = what

    def __enter__(self):
        self.began = time.monotonic()
        return self

    def __exit__(self, *failure) -> None:
        took = round(time.monotonic() - self.began, 2)
        if took < SLOW:
            return
        DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
        with open(DIAGNOSTICS / "slow-steps.jsonl", "a", encoding="utf-8") as out:
            out.write(json.dumps({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "step": self.what,
                "seconds": took, "during": os.environ.get("PYTEST_CURRENT_TEST", ""),
                "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
                "failed": failure[0].__name__ if failure[0] else None}) + "\n")


def watch(page):
    """Keep an account of every request `page` makes."""
    page.traffic = {}

    def began(request):
        page.traffic[request] = {"url": request.url, "began": time.monotonic(),
                                 "took": None, "ended": "never finished"}

    def ended(request, how):
        entry = page.traffic.get(request)
        if entry is not None:
            entry["took"] = round(time.monotonic() - entry["began"], 3)
            entry["ended"] = how

    page.on("request", began)
    page.on("requestfinished", lambda r: ended(r, "finished"))
    page.on("requestfailed", lambda r: ended(r, f"failed: {r.failure or 'no reason given'}"))
    _OPEN.append(page)
    return page


def account_of(page) -> dict:
    """What `page` asked for, from whom, and what it was still waiting on."""
    now = time.monotonic()
    hosts: dict[str, dict] = {}
    waiting, failed = [], []
    for entry in getattr(page, "traffic", {}).values():
        parts = urlsplit(entry["url"])
        where = f"{parts.netloc}{parts.path}"[:140]
        host = hosts.setdefault(parts.netloc, {"requests": 0, "slowest_seconds": 0.0,
                                               "never_finished": 0, "failed": 0})
        took = entry["took"] if entry["took"] is not None else round(now - entry["began"], 3)
        host["requests"] += 1
        host["slowest_seconds"] = max(host["slowest_seconds"], took)
        if entry["ended"] == "never finished":
            host["never_finished"] += 1
            waiting.append({"what": where, "waited_seconds": took})
        elif entry["ended"].startswith("failed"):
            host["failed"] += 1
            failed.append({"what": where, "how": entry["ended"]})
    try:
        url = page.url
    except Exception:
        url = "(the page was already closed)"
    return {"page": url, "hosts": hosts, "still_waiting_for": waiting[:40],
            "failed": failed[:40]}


def _end_of(path: Path, lines: int = 40) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["(no log)"]
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in text.splitlines()[-lines:]]


def write_account(test: str, failure: str, pages: list) -> Path:
    DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", test).strip("-")[-120:]
    target = DIAGNOSTICS / f"{time.strftime('%Y%m%dT%H%M%S')}-{name}.json"
    target.write_text(json.dumps({
        "test": test, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "failure": failure.splitlines()[-25:],
        "pages": [account_of(page) for page in pages],
        "logs": {what: _end_of(path) for what, path in _LOGS.items()},
    }, indent=1), encoding="utf-8")
    return target


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if "browser" not in Path(str(item.fspath)).parts:
        return
    if report.when == "call" and report.failed and not hasattr(report, "wasxfail"):
        written = write_account(item.nodeid, str(report.longrepr), list(_OPEN))
        report.sections.append(("what the pages were waiting for", str(written)))
    if report.when == "teardown":
        _OPEN.clear()
sys.path.insert(0, str(REPO / "tests"))
from live_server import LiveServer  # noqa: E402

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="browser tests need the playwright package")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        launched = None
        with timed("starting the browser"):
            for options in ({}, {"channel": "chrome"}):
                try:
                    launched = p.chromium.launch(**options)
                    break
                except Exception:
                    continue
        if launched is None:
            pytest.skip("no Chromium or Chrome available to drive")
        yield launched
        with timed("closing the browser"):
            launched.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not DIST.is_file():
        pytest.skip("web/dist is not built: run `npm ci && npm run build` in web/")
    with timed("starting the API server"):
        live = LiveServer(tmp_path_factory.mktemp("scrivio-browser")).start()
    try:
        yield live
    finally:
        with timed("stopping the API server"):
            live.stop()


@pytest.fixture
def page(browser, server):
    context = browser.new_context()
    context.add_cookies([server.session_cookie()])
    page = watch(context.new_page())
    _LOGS["the API server"] = server.log
    page.requested, page.dialogs, page.console_errors = [], [], []
    page.on("request", lambda r: page.requested.append(r.url))
    page.on("dialog", lambda d: (page.dialogs.append(d.message), d.dismiss()))
    page.on("pageerror", lambda e: page.console_errors.append(str(e)))
    yield page
    context.close()


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    """Demo mode: the whole interface works, on canned model output, so
    the workflows can be driven end to end without a provider."""
    if not DIST.is_file():
        pytest.skip("web/dist is not built: run `npm ci && npm run build` in web/")
    with timed("starting the demo API server"):
        live = LiveServer(tmp_path_factory.mktemp("scrivio-demo"), demo=True).start()
    try:
        yield live
    finally:
        with timed("stopping the demo API server"):
            live.stop()


@pytest.fixture(scope="module")
def dev_server(demo_server):
    """The Vite development server, proxying to the demo backend: the
    setup the README tells a contributor to use."""
    import os
    import shutil
    import subprocess
    import time
    import urllib.request

    from live_server import free_port

    web = REPO / "web"
    npx = shutil.which("npx")
    if npx is None or not (web / "node_modules" / "vite").is_dir():
        pytest.skip("the frontend dependencies are not installed: run `npm ci` in web/")
    port = free_port()
    # What it says is kept. It used to be thrown away, and a page that
    # would not load from it left nothing to read.
    log = demo_server.root / "vite.log"
    _LOGS["the Vite development server"] = log
    with open(log, "ab") as out:
        process = subprocess.Popen(
            [npx, "vite", "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
            cwd=web, env={**os.environ, "SCRIVIO_BACKEND": demo_server.base},
            stdout=out, stderr=out)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            if process.poll() is not None:
                pytest.fail("the Vite dev server exited during startup")
            try:
                urllib.request.urlopen(base, timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.fail("the Vite dev server did not start")
        yield base
    finally:
        with timed("stopping the Vite development server"):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def paired_page(browser, server, base=None):
    context = browser.new_context(accept_downloads=True)
    cookie = server.session_cookie()
    if base:
        cookie["url"] = base
    context.add_cookies([cookie])
    page = watch(context.new_page())
    page.requested, page.dialogs, page.console_errors = [], [], []
    page.html_instead_of_data = []
    _LOGS["the API server"] = server.log
    page.on("dialog", lambda d: (page.dialogs.append(d.message), d.accept()))
    page.on("pageerror", lambda e: page.console_errors.append(str(e)))

    def note(response):
        page.requested.append(response.url)
        kind = response.headers.get("content-type", "")
        path = "/" + response.url.split("/", 3)[3].split("?")[0] if response.url.count("/") > 2 else "/"
        api_like = path.split("/")[1] in (
            "auth", "mode", "settings", "generate", "clarify", "jobs", "articles",
            "interviews", "job-profiles", "resumes", "transcribe", "speak")
        if api_like and "text/html" in kind:
            page.html_instead_of_data.append(path)
    page.on("response", note)
    return context, page
