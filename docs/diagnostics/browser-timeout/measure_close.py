"""Why does closing the browser take so long at the end of some test files?

Two measurements, neither of which runs a test.

`close`: start the browser, open one blank page, hold it for a number of
seconds, close the browser, and write down how long the close took and
what the throwaway profile held just before. Nothing of Scrivio's is
involved in this one.

`loads`: start a demo server on a store of its own, and load its home
page again and again for the first minute of a browser's life, to see
whether pages are slow to load during the seconds in which a close is.

    python docs/diagnostics/browser-timeout/measure_close.py close <folder>
    python docs/diagnostics/browser-timeout/measure_close.py loads <folder>
"""
import json, os, platform, re, subprocess, sys, tempfile, time
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))
from playwright.sync_api import sync_playwright

HELD = (2, 8, 15, 20, 25, 30, 35, 40, 50, 60)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def load() -> list[float]:
    return [round(x, 2) for x in os.getloadavg()]


def machine() -> dict:
    try:
        chrome = subprocess.run([CHROME, "--version"], capture_output=True,
                                text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        chrome = "not found where it is looked for"
    return {"system": f"{platform.system()} {platform.release()}",
            "browser": chrome, "playwright": metadata.version("playwright"),
            "browser_is": "the installed Google Chrome, on a new profile each time it is started"}


def profile() -> Path | None:
    """The folder Playwright made for the browser it started last."""
    listed = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
    found = re.findall(r"--user-data-dir=(\S*playwright_chromiumdev_profile-\S+)", listed)
    return Path(found[-1]) if found else None


def weigh(folder: Path | None) -> dict:
    if folder is None:
        return {"megabytes": None, "largest": []}
    sizes: dict[str, int] = {}
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                top = path.relative_to(folder).parts[0]
                sizes[top] = sizes.get(top, 0) + path.stat().st_size
        except OSError:
            pass
    largest = sorted(sizes.items(), key=lambda kv: -kv[1])[:3]
    return {"megabytes": round(sum(sizes.values()) / 1e6, 1),
            "largest": [{"folder": k, "megabytes": round(v / 1e6, 1)} for k, v in largest]}


def close(out: Path) -> None:
    rows = []
    for held in HELD:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_context().new_page()
            page.goto("about:blank")
            page.wait_for_timeout(held * 1000)
            held_profile = weigh(profile())
            began = time.monotonic()
            browser.close()
            took = round(time.monotonic() - began, 2)
        rows.append({"held_open_s": held, "close_s": took,
                     "open_and_close_s": round(held + took, 1),
                     "profile": held_profile, "load_average": load()})
        print(json.dumps(rows[-1]), flush=True)
    (out / f"{time.strftime('%Y-%m-%d')}-close-by-time-open.json").write_text(json.dumps({
        "what": "how long closing the browser took, by how long it had been open, "
                "with one blank page and nothing of Scrivio's",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "machine": machine(), "rows": rows,
    }, indent=1) + "\n", encoding="utf-8")


def loads(out: Path) -> None:
    from live_server import LiveServer

    with tempfile.TemporaryDirectory() as root:
        live = LiveServer(Path(root) / "store", demo=True).start()
        took = []
        try:
            with sync_playwright() as p:
                started = time.monotonic()
                browser = p.chromium.launch(channel="chrome")
                while time.monotonic() - started < 60:
                    context = browser.new_context()
                    context.add_cookies([live.session_cookie()])
                    page = context.new_page()
                    at = time.monotonic() - started
                    began = time.monotonic()
                    page.goto(live.base + "/")
                    page.get_by_role("heading", level=1).wait_for()
                    took.append((at, round(time.monotonic() - began, 2)))
                    context.close()
                browser.close()
        finally:
            live.stop()

    def part(low: int, high: int) -> dict:
        these = sorted(s for at, s in took if low <= at < high)
        return {"from_s": low, "to_s": high, "loads": len(these),
                "median_s": these[len(these) // 2] if these else None,
                "slowest_s": these[-1] if these else None}

    report = {"what": "how long Scrivio's home page took to load, by how long the "
                      "browser had been open, from a demo server on a store of its own",
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "machine": machine(),
              "parts": [part(0, 15), part(15, 45), part(45, 60)], "load_average": load()}
    print(json.dumps(report, indent=1))
    (out / f"{time.strftime('%Y-%m-%d')}-loads-by-time-open.json").write_text(
        json.dumps(report, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("close", "loads"):
        sys.exit(__doc__)
    target = Path(sys.argv[2])
    target.mkdir(parents=True, exist_ok=True)
    {"close": close, "loads": loads}[sys.argv[1]](target)
