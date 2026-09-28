"""What is ready, before you wait for something to fail.

    python -m api.doctor

Checks the install, not the models: nothing here calls a provider or
spends anything. It exits 0 when Scrivio can start and do real work, 1
when something needed is missing, and says which.

No check prints a credential. A key is reported as set or not set.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUPPORTED_PYTHON = ((3, 12), (3, 13))
MINIMUM_NODE = 20

OK, WARN, FAIL = "ok", "note", "PROBLEM"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, status: str, what: str, found: str, fix: str = "") -> None:
        self.rows.append((status, what, found, fix))

    @property
    def failed(self) -> bool:
        return any(row[0] == FAIL for row in self.rows)

    def show(self, stream=None) -> None:
        stream = stream or sys.stdout
        width = max(len(row[1]) for row in self.rows)
        for status, what, found, fix in self.rows:
            print(f"  [{status:^7}] {what:<{width}}  {found}", file=stream)
            if fix and status != OK:
                print(f"  {'':9} {'':<{width}}  -> {fix}", file=stream)


def check_python(report: Report) -> None:
    have = sys.version_info[:2]
    found = f"{have[0]}.{have[1]}"
    if have in SUPPORTED_PYTHON:
        report.add(OK, "Python", found)
    else:
        tested = ", ".join(f"{a}.{b}" for a, b in SUPPORTED_PYTHON)
        report.add(WARN if have > SUPPORTED_PYTHON[0] else FAIL, "Python", found,
                   f"tested on {tested}. The pinned dependencies do not "
                   "install on 3.10; 3.11 has not been tested.")


def check_packages(report: Report) -> None:
    missing = []
    for module in ("fastapi", "uvicorn", "pydantic", "dotenv", "anthropic", "openai",
                   "httpx", "bs4", "readability", "pypdf", "docx", "fpdf"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    if missing:
        report.add(FAIL, "Python packages", "missing: " + ", ".join(missing),
                   "pip install -r requirements.txt -c constraints.txt")
    else:
        report.add(OK, "Python packages", "all present")


def check_interface(report: Report) -> None:
    built = REPO / "web" / "dist" / "index.html"
    if built.is_file():
        sources = list((REPO / "web" / "src").rglob("*.ts*"))
        newest = max((p.stat().st_mtime for p in sources), default=0)
        if newest > built.stat().st_mtime:
            report.add(WARN, "Interface", "built, but older than its source",
                       "cd web && npm run build")
        else:
            report.add(OK, "Interface", "built")
        return
    node = shutil.which("node")
    version = ""
    if node:
        try:
            version = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=10
            ).stdout.strip().lstrip("v")
        except Exception:
            version = ""
    if not node:
        fix = f"install Node.js {MINIMUM_NODE} or newer, then: cd web && npm ci && npm run build"
    elif version and int(version.split(".")[0]) < MINIMUM_NODE:
        fix = f"Node.js {version} is too old; install {MINIMUM_NODE} or newer"
    else:
        fix = "cd web && npm ci && npm run build"
    report.add(FAIL, "Interface",
               "not built, so the older classic interface would be served", fix)


def check_provider(report: Report) -> None:
    from pipeline.providers.claude_cli_adapter import cli_status
    from pipeline.runtime_mode import demo_mode

    if demo_mode():
        report.add(WARN, "Provider", "demo mode: canned examples, no model is called",
                   "unset SCRIVIO_DEMO for real results")
        return
    found = [name for name, key in (("Anthropic", "ANTHROPIC_API_KEY"),
                                    ("OpenAI", "OPENAI_API_KEY")) if os.environ.get(key)]
    cli = cli_status()
    if cli["state"] != "not installed":
        found.append(f"{cli['cli']} command line ({cli['state']})")
    if found:
        report.add(OK, "Provider", ", ".join(found))
        if cli["state"] == "installed, not yet used" and len(found) == 1:
            report.add(WARN, "Provider sign-in",
                       "installed is not the same as signed in, and only a "
                       "real call can tell",
                       f"run `{cli['cli']}` once in a terminal to confirm")
    else:
        report.add(FAIL, "Provider", "none configured",
                   "add a key to .env, or sign in to a command-line assistant, "
                   "or use SCRIVIO_DEMO=1 to look around")


def check_extras(report: Report) -> None:
    search = [k for k in ("TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY", "EXA_API_KEY")
              if os.environ.get(k)]
    report.add(OK if search else WARN, "Web search",
               "configured" if search else "no search key",
               "optional: articles and interview research use it")
    report.add(OK if os.environ.get("OPENAI_API_KEY") else WARN, "Voice",
               "server voice and transcription" if os.environ.get("OPENAI_API_KEY")
               else "browser voice and dictation only",
               "optional: set OPENAI_API_KEY for the better voice")
    from render.mermaid_worker import diagram_renderer
    renderer = diagram_renderer()
    pinned = renderer and ".scrivio" in renderer[0]
    report.add(OK if pinned else WARN, "Diagram renderer",
               "installed and pinned" if pinned
               else "found, not pinned" if renderer and "npx" not in renderer[0]
               else "not installed: diagrams are left out of articles",
               "optional: python scripts/setup_renderers.py")


def check_storage(report: Report) -> None:
    from api import boundary

    output = Path(os.environ.get("ARTICLE_OUTPUT_DIR", "./output"))
    try:
        output.mkdir(parents=True, exist_ok=True)
        probe = output / ".doctor-write-test"
        probe.write_text("ok")
        probe.unlink()
        report.add(OK, "Output folder", f"{output} is writable")
    except OSError:
        report.add(FAIL, "Output folder", f"{output} cannot be written to",
                   "set ARTICLE_OUTPUT_DIR to a folder you own")
    key = boundary.state_dir() / "session.key"
    if key.is_file():
        mode = stat.S_IMODE(key.stat().st_mode)
        report.add(OK if mode == 0o600 else FAIL, "Session key",
                   "readable only by you" if mode == 0o600
                   else f"readable by others (mode {oct(mode)})",
                   f"chmod 600 {key}")
    else:
        report.add(OK, "Session key", "will be created on first start")
    settings = Path(os.environ.get("SCRIVIO_ENV_FILE") or REPO / ".env")
    if settings.is_file():
        mode = stat.S_IMODE(settings.stat().st_mode)
        report.add(OK if not mode & 0o077 else WARN, "Settings file",
                   "readable only by you" if not mode & 0o077
                   else "readable by other accounts on this machine",
                   f"chmod 600 {settings}")


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.environ.get("SCRIVIO_ENV_FILE") or REPO / ".env",
                    override=True, interpolate=False)
    except Exception:
        pass
    report = Report()
    check_python(report)
    check_packages(report)
    if not report.failed:
        check_interface(report)
        check_provider(report)
        check_extras(report)
        check_storage(report)
    print("\nScrivio setup check\n")
    report.show()
    print()
    if report.failed:
        print("  Not ready. Fix the lines marked PROBLEM, then run this again.\n")
        return 1
    print("  Ready. Start it with:  python -m api\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
