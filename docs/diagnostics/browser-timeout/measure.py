"""What does loading a page from the Vite development server wait on?

Starts the same two servers the browser tests start, loads the page the
failing test loads, and writes down every request the page made: where
to, how long it took, whether it finished. Run with the dependency cache
as it is ("warm") or moved aside ("cold"), and with the font host
answering, refusing, or never answering.
"""
import json, os, shutil, subprocess, sys, time, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO))
from live_server import LiveServer, free_port
from playwright.sync_api import sync_playwright

out = Path(sys.argv[1]); mode = sys.argv[2]; fonts = sys.argv[3]; loads = int(sys.argv[4])
out.mkdir(parents=True, exist_ok=True)
web = REPO / "web"
cache, aside = web / "node_modules" / ".vite", web / "node_modules" / ".vite-set-aside"
if mode == "cold" and cache.is_dir():
    cache.rename(aside)

backend = LiveServer(out / "backend", demo=True).start()
port = free_port()
log = open(out / "vite.log", "wb")
started = time.monotonic()
vite = subprocess.Popen([shutil.which("npx"), "vite", "--port", str(port), "--strictPort",
                         "--host", "127.0.0.1", "--debug", "deps"],
                        cwd=web, env={**os.environ, "SCRIVIO_BACKEND": backend.base},
                        stdout=log, stderr=log)
base = f"http://127.0.0.1:{port}"
for _ in range(300):
    try:
        urllib.request.urlopen(base, timeout=1).read(); break
    except Exception:
        time.sleep(0.1)
answering_after = time.monotonic() - started
report = {"mode": mode, "fonts": fonts, "dev_server_answering_after_s": round(answering_after, 2),
          "loads": []}
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for n in range(loads):
            context = browser.new_context()
            cookie = backend.session_cookie(); cookie["url"] = base
            context.add_cookies([cookie])
            page = context.new_page()
            seen, begun = {}, {}
            if fonts == "refused":
                context.route("**://fonts.googleapis.com/**", lambda r: r.abort())
                context.route("**://fonts.gstatic.com/**", lambda r: r.abort())
            if fonts == "never-answers":
                context.route("**://fonts.googleapis.com/**", lambda r: None)   # held for ever
            page.on("request", lambda r: begun.setdefault(r, time.monotonic()))
            def done(r, how):
                seen[r] = (how, round(time.monotonic() - begun.get(r, time.monotonic()), 3))
            page.on("requestfinished", lambda r: done(r, "finished"))
            page.on("requestfailed", lambda r: done(r, "failed: " + (r.failure or "")))
            marks = {}
            page.on("domcontentloaded", lambda: marks.setdefault("domcontentloaded", round(time.monotonic() - t0, 3)))
            page.on("load", lambda: marks.setdefault("load", round(time.monotonic() - t0, 3)))
            reloads = []
            page.on("framenavigated", lambda f: reloads.append(round(time.monotonic() - t0, 3)) if f == page.main_frame else None)
            t0 = time.monotonic()
            outcome = "loaded"
            try:
                page.goto(f"{base}/#/desk", timeout=30000)
            except Exception as error:
                outcome = str(error).splitlines()[0][:120]
            took = round(time.monotonic() - t0, 3)
            page.wait_for_timeout(300)
            by_host, slowest, unfinished = {}, [], []
            for r, t in begun.items():
                host = urlsplit(r.url).netloc
                how, secs = seen.get(r, ("NEVER FINISHED", round(time.monotonic() - t, 3)))
                h = by_host.setdefault(host, {"requests": 0, "slowest_s": 0.0, "failed": 0, "unfinished": 0})
                h["requests"] += 1; h["slowest_s"] = max(h["slowest_s"], secs)
                h["failed"] += how.startswith("failed"); h["unfinished"] += how == "NEVER FINISHED"
                slowest.append((secs, how, r.url.replace(base, "")[:90]))
                if how == "NEVER FINISHED":
                    unfinished.append(r.url.replace(base, "")[:90])
            report["loads"].append({
                "n": n + 1, "goto": outcome, "goto_took_s": took, **marks,
                "main_frame_navigations_at_s": reloads, "requests": len(begun),
                "by_host": by_host, "unfinished": unfinished[:8],
                "five_slowest": [list(x) for x in sorted(slowest, reverse=True)[:5]]})
            context.close()
        browser.close()
finally:
    vite.terminate()
    try: vite.wait(timeout=5)
    except Exception: vite.kill()
    log.close(); backend.stop()
    if mode == "cold" and aside.is_dir():
        if cache.is_dir():
            shutil.rmtree(aside)          # a new cache was built: the old one is not needed
        else:
            aside.rename(cache)
(out / "report.json").write_text(json.dumps(report, indent=1))
for l in report["loads"]:
    print(f"{mode:4} fonts={fonts:13} load {l['n']}: goto {l['goto_took_s']:6.2f}s  dcl {l.get('domcontentloaded')}  load {l.get('load')}  "
          f"requests {l['requests']}  navigations {len(l['main_frame_navigations_at_s'])}  -> {l['goto']}")
    for host, h in l["by_host"].items():
        print(f"       {host:28} {h['requests']:4} requests, slowest {h['slowest_s']:.2f}s, failed {h['failed']}, unfinished {h['unfinished']}")
