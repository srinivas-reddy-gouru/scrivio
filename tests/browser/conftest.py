"""A real server and a real browser, both on synthetic data.

These tests exist because a parser test can only say what the parser
emits. Whether anything RUNS is a question for a browser, asked through
the same code path a reader would take to open an article.

Nothing here touches the developer's resumes, articles, settings, or
provider keys: the server gets its own output directory, its own state
directory, and a settings file that does not exist.

Skipped, not failed, when there is no browser to drive or no built
interface to load. CI installs both (see .github/workflows).
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "web" / "dist" / "index.html"

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="browser tests need the playwright package")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
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


class Server:
    def __init__(self, base: str, output: Path, state: Path):
        self.base, self.output, self.state = base, output, state

    def session_cookie(self) -> dict:
        """A session minted with the server's own key: the test is a
        paired browser, by the same mechanism as any other."""
        from api import boundary
        previous = os.environ.get("SCRIVIO_STATE_DIR")
        os.environ["SCRIVIO_STATE_DIR"] = str(self.state)
        try:
            value = boundary.mint_session()
        finally:
            if previous is None:
                os.environ.pop("SCRIVIO_STATE_DIR", None)
            else:
                os.environ["SCRIVIO_STATE_DIR"] = previous
        return {"name": boundary.COOKIE_NAME, "value": value, "url": self.base}


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    if not DIST.is_file():
        pytest.skip("web/dist is not built: run `npm ci && npm run build` in web/")
    root = tmp_path_factory.mktemp("scrivio-browser")
    output, state = root / "output", root / "state"
    output.mkdir()
    port = _free_port()
    env = {
        k: v for k, v in os.environ.items()
        if not k.endswith("_API_KEY") and k not in ("LLM_PROVIDER", "LLM_CLI")
    }
    env.update({
        "PORT": str(port), "SCRIVIO_HOST": "127.0.0.1",
        "ARTICLE_OUTPUT_DIR": str(output), "SCRIVIO_STATE_DIR": str(state),
        "SCRIVIO_ENV_FILE": str(root / "no-such.env"),
        "PYTHONPATH": str(REPO),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "api"], cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail("the server exited during startup")
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            pytest.fail("the server did not start")
        yield Server(base, output, state)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


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
