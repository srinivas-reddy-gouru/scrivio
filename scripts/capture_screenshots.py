"""Capture the README screenshots, from invented data only.

A screenshot in a public README is kept forever, at full resolution, by
everyone who clones the repository. So this script never looks at a
running instance of yours. It starts a server of its own, in demo mode,
on an empty folder it creates and then removes, and everything in the
pictures is made by driving that server through the interface:

  - the person and the posting are the built-in sample, which is invented
  - the model output is the demo examples, and the page says so in a
    banner that is in the picture

What the pictures show is therefore what the interface looks like and how
the workflows go. They do not show what a model would write for you, and
the README does not say they do.

    (cd web && npm ci && npm run build)
    python scripts/capture_screenshots.py            # --light for light theme too
    python scripts/capture_screenshots.py --only resume-studio

Needs the playwright package and Chrome or Chromium.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs"
VIEWPORT = {"width": 1440, "height": 1000}
NAMES = ["home", "resume-studio", "job-prep", "topic-practice", "coding-round",
         "article-studio"]


class DemoServer:
    """A demo-mode server on a folder of its own. It is given no keys and
    no settings file, so there is nothing of the owner's it could show."""

    def __init__(self, root: Path):
        self.root = root
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.environment = {
            **{k: v for k, v in os.environ.items()
               if not k.endswith("_API_KEY") and k != "LLM_PROVIDER"},
            "SCRIVIO_DEMO": "1", "SCRIVIO_HOST": "127.0.0.1", "PORT": str(self.port),
            "ARTICLE_OUTPUT_DIR": str(root / "output"),
            "SCRIVIO_STATE_DIR": str(root / "state"),
            "SCRIVIO_ENV_FILE": str(root / "no-such.env"),
            "PYTHONPATH": str(REPO),
        }
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> "DemoServer":
        self.process = subprocess.Popen(
            [sys.executable, "-m", "api"], cwd=REPO, env=self.environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            if self.process.poll() is not None:
                raise SystemExit("the demo server exited during startup")
            try:
                urllib.request.urlopen(f"{self.base}/health", timeout=1).read()
                return self
            except OSError:
                time.sleep(0.1)
        raise SystemExit("the demo server did not start")

    def __exit__(self, *_) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def session_cookie(self) -> dict:
        """Pair the way the tests do: mint a session with this server's
        own key. The key is in the folder this script made."""
        minted = subprocess.run(
            [sys.executable, "-c",
             "from api import boundary; print(boundary.COOKIE_NAME); "
             "print(boundary.mint_session())"],
            cwd=REPO, env=self.environment, capture_output=True, text=True, check=True)
        name, value = minted.stdout.split()[-2:]
        return {"name": name, "value": value, "url": self.base}


def launch(playwright):
    for options in ({"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(**options)
        except Exception:
            continue
    raise SystemExit("no Chrome or Chromium available to drive")


def capture(theme: str, only: str | None) -> None:
    from playwright.sync_api import sync_playwright

    if not (REPO / "web" / "dist" / "index.html").is_file():
        raise SystemExit("web/dist is not built: run `npm ci && npm run build` in web/")

    suffix = "" if theme == "dark" else "-light"
    want = (lambda name: only is None or name == only)
    root = Path(tempfile.mkdtemp(prefix="scrivio-screenshots-"))

    def shot(page, name: str) -> None:
        page.wait_for_timeout(900)
        assert page.locator(".mode-banner.demo").is_visible(), \
            f"{name}: the demo label is not in the picture"
        page.screenshot(path=str(OUT / f"{name}{suffix}.png"))
        print(f"  wrote docs/{name}{suffix}.png")

    try:
        with DemoServer(root) as server, sync_playwright() as p:
            browser = launch(p)
            context = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
            context.add_cookies([server.session_cookie()])
            page = context.new_page()
            page.goto(f"{server.base}/")
            page.get_by_role("heading", level=1).wait_for()
            # The toggle persists in localStorage, so set the theme once.
            if page.evaluate("document.documentElement.dataset.theme") != theme:
                page.click(".theme-btn")
                page.wait_for_timeout(500)

            print(f"{theme} theme:")
            # Home first, while nothing is stored: this is the page a new
            # install opens on, which is what the quickstart leads to.
            if want("home"):
                shot(page, "home")

            if want("resume-studio") or want("job-prep"):
                page.get_by_role("button", name="Try it with a sample resume").click()
                page.get_by_role("button", name="Read my resume").click()
                page.get_by_role("button", name="Tailor it to this JD").click(timeout=30000)
                page.locator(".metric-chip").first.wait_for(timeout=30000)
            if want("resume-studio"):
                shot(page, "resume-studio")

            if want("job-prep"):
                sample = page.evaluate("""async () => {
                    const all = await fetch('/resumes').then(r => r.json());
                    const doc = await fetch('/resumes/' + all[0].resume_id).then(r => r.json());
                    return {resume: doc.original_text || '', jd: doc.jd_text || ''};
                }""")
                page.goto(f"{server.base}/#/job")
                page.get_by_role("button", name="Add a job target").click()
                page.get_by_label("Role title", exact=True).fill("Senior Backend Engineer")
                page.get_by_label("Company", exact=True).fill("Example Payments Inc")
                page.get_by_label("Job description text", exact=True).fill(sample["jd"])
                page.get_by_label("Your resume text", exact=True).fill(sample["resume"])
                page.get_by_role("button", name="Analyze my fit").click()
                page.get_by_role("button", name="Take the screen").wait_for(timeout=30000)
                shot(page, "job-prep")

            for name, mode in (("topic-practice", "Practice"), ("coding-round", "Coding")):
                if not want(name):
                    continue
                page.goto(f"{server.base}/#/interview")
                page.get_by_label("Interview topic").fill(
                    "Kafka consumer groups" if mode == "Practice" else "arrays and intervals")
                page.locator(".seg-pill", has_text=mode).first.click()
                page.get_by_role("button", name="Take a seat").click()
                page.get_by_label("Your answer").or_(
                    page.get_by_label("Your code")).first.wait_for(timeout=30000)
                shot(page, name)
                page.goto(f"{server.base}/")

            if want("article-studio"):
                page.goto(f"{server.base}/#/newsroom")
                page.get_by_label("Article topic or question").fill(
                    "How Kafka consumer group rebalancing works, and what pauses during it")
                page.get_by_role("button", name="Generate").click()
                page.locator("article.prose h1").wait_for(timeout=90000)
                shot(page, "article-studio")

            browser.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--light", action="store_true", help="also capture light theme")
    parser.add_argument("--only", choices=NAMES, help="capture just this one")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    capture("dark", args.only)
    if args.light:
        capture("light", args.only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
