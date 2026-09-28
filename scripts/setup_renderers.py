"""Install the diagram renderer, once, at a pinned version.

    python scripts/setup_renderers.py            # install
    python scripts/setup_renderers.py --check    # report what is installed

Scrivio checks every generated diagram by rendering it before the
diagram is allowed into an article. The renderer is mermaid-cli, which
drives a headless browser.

It used to be fetched with `npx -y` in the middle of each article job:
whatever version was newest that day, downloaded and executed while the
user waited. This script is the replacement. It installs one named
version into .scrivio/tools/, which is ignored by git, and nothing is
downloaded at job time again.

This downloads a browser build of roughly 150 MB. It is a separate step
from `npm ci` so that installing the interface does not pull that in
for people who do not generate articles with diagrams.

Terminal recordings (VHS) are not installed here or anywhere. They are
disabled: see render/vhs_worker.py.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / ".scrivio" / "tools"
PACKAGE = "@mermaid-js/mermaid-cli"
VERSION = "12.0.0"          # change deliberately, then re-run and re-test


def installed() -> str | None:
    manifest = TOOLS / "node_modules" / PACKAGE / "package.json"
    if not manifest.is_file():
        return None
    import json
    return json.loads(manifest.read_text()).get("version")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    have = installed()
    if args.check:
        sys.path.insert(0, str(REPO))
        from render.mermaid_worker import diagram_renderer
        print(f"pinned version : {VERSION}")
        print(f"installed here : {have or 'no'}")
        print(f"renderer in use: {diagram_renderer() or 'none, diagrams will be left out'}")
        return 0 if have == VERSION else 1

    npm = shutil.which("npm")
    if npm is None:
        print("npm was not found. Install Node.js 20 or newer, then run this again.")
        return 2
    if have == VERSION:
        print(f"{PACKAGE} {VERSION} is already installed.")
        return 0
    TOOLS.mkdir(parents=True, exist_ok=True)
    print(f"Installing {PACKAGE}@{VERSION} into {TOOLS.relative_to(REPO)} ...")
    result = subprocess.run(
        [npm, "install", "--prefix", str(TOOLS), "--no-audit", "--no-fund",
         "--save-exact", f"{PACKAGE}@{VERSION}"],
        check=False,
    )
    if result.returncode != 0:
        print("The install failed. Nothing about Scrivio changed: diagrams "
              "will be left out of articles until a renderer is installed.")
        return result.returncode
    print("Done. Diagrams will be checked with this renderer from now on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
