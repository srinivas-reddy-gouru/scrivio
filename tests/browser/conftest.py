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
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "web" / "dist" / "index.html"
sys.path.insert(0, str(REPO / "tests"))
from live_server import LiveServer  # noqa: E402

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="browser tests need the playwright package")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        launched = None
        for options in ({}, {"channel": "chrome"}):
            try:
                launched = p.chromium.launch(**options)
                break
            except Exception:
                continue
        if launched is None:
            pytest.skip("no Chromium or Chrome available to drive")
        yield launched
        launched.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not DIST.is_file():
        pytest.skip("web/dist is not built: run `npm ci && npm run build` in web/")
    live = LiveServer(tmp_path_factory.mktemp("scrivio-browser")).start()
    try:
        yield live
    finally:
        live.stop()


@pytest.fixture
def page(browser, server):
    context = browser.new_context()
    context.add_cookies([server.session_cookie()])
    page = context.new_page()
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
    live = LiveServer(tmp_path_factory.mktemp("scrivio-demo"), demo=True).start()
    try:
        yield live
    finally:
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
    process = subprocess.Popen(
        [npx, "vite", "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
        cwd=web, env={**os.environ, "SCRIVIO_BACKEND": demo_server.base},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    page = context.new_page()
    page.requested, page.dialogs, page.console_errors = [], [], []
    page.html_instead_of_data = []
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
