"""Capture the README screenshots from the running app.

Screenshots go stale faster than prose, and a README showing a UI that no
longer exists is worse than one with no pictures. This drives the real
app against real saved work, so re-running it after a UI change is the
whole maintenance story.

    python -m uvicorn api.server:app --port 8899   # in another shell
    python scripts/capture_screenshots.py

Add --light for the light-theme variants.

The resume shot is the one that must never be taken against real work:
it shows a contact line and an employment history at full resolution. Run
that one against the fictional resume instead, on a server pointed at an
output directory of its own so nothing personal is even loadable:

    python scripts/demo_resume.py /tmp/demo-output
    ARTICLE_OUTPUT_DIR=/tmp/demo-output python -m uvicorn api.server:app --port 8897
    python scripts/capture_screenshots.py --base http://localhost:8897 --only resume-studio
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8899"
OUT = Path(__file__).resolve().parent.parent / "docs"
VIEWPORT = {"width": 1440, "height": 1000}


def settle(page, ms: int = 1200) -> None:
    page.wait_for_timeout(ms)


def shot(page, name: str) -> None:
    OUT.mkdir(exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"  wrote docs/{name}.png")


def capture(theme: str, base: str = BASE, only: str | None = None) -> None:
    suffix = "" if theme == "dark" else "-light"
    want = (lambda name: only is None or name == only)
    with sync_playwright() as p:
        # Drive the Chrome already on the machine: Playwright's own
        # bundled browser is a 150MB download for no visual difference.
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        page.goto(f"{base}/#/floor")
        settle(page)
        # The toggle persists in localStorage, so set the theme once.
        current = page.evaluate("document.documentElement.dataset.theme")
        if current != theme:
            page.click(".theme-btn")
            settle(page, 500)

        print(f"{theme} theme:")
        if want("home"):
            shot(page, f"home{suffix}")

        if want("article-studio"):
            page.goto(f"{base}/#/newsroom")
            settle(page)
            shot(page, f"article-studio{suffix}")

        if want("topic-practice"):
            page.goto(f"{base}/#/interview")
            settle(page)
            shot(page, f"topic-practice{suffix}")

        if want("job-prep"):
            page.goto(f"{base}/#/job")
            settle(page)
            shot(page, f"job-prep{suffix}")

        # A coding round only exists once one has been run, and creating
        # one costs a real interviewer call. Shoot the newest if present,
        # and say plainly when there is none rather than leaving a stale
        # image in docs/ pretending to be current.
        coding = None if not want("coding-round") else page.evaluate("""async () => {
            const all = await fetch('/interviews').then(r => r.json());
            return (all.find(s => s.mode === 'coding') || {}).session_id || null;
        }""")
        if coding:
            page.evaluate(
                "id => { sessionStorage.setItem('studio-open-session', id);"
                " window.dispatchEvent(new Event('studio-open-session')); }", coding)
            settle(page, 2500)
            shot(page, f"coding-round{suffix}")
        elif want("coding-round"):
            print("  skipped coding-round.png: no coding session on file")

        # The resume desk's flagship view is the tailored paper with its
        # honesty notes, which lives behind two clicks rather than a URL.
        if not want("resume-studio"):
            browser.close()
            return
        page.goto(f"{base}/#/desk")
        settle(page)
        opened = page.query_selector("//button[text()='Open']")
        if opened:
            opened.click()
            settle(page, 2200)
            tailor = page.query_selector("//button[contains(., 'Tailor')]")
            if tailor and not tailor.is_disabled():
                tailor.click()
                settle(page, 1500)
                note = page.query_selector(".score-band button")
                if note:
                    note.click()          # open the first honesty note
                    settle(page, 900)
        shot(page, f"resume-studio{suffix}")

        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--light", action="store_true", help="also capture light theme")
    parser.add_argument("--base", default=BASE, help="server to drive (default %(default)s)")
    parser.add_argument("--only", help="capture just this one, e.g. resume-studio")
    args = parser.parse_args()
    capture("dark", args.base, args.only)
    if args.light:
        capture("light", args.base, args.only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
