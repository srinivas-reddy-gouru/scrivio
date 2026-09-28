"""Back up and restore from the command line.

    python -m api.data backup  scrivio-backup.zip
    python -m api.data restore scrivio-backup.zip [--replace]
    python -m api.data show

The server does not need to be running. Restore leaves existing records
alone unless --replace is given, and with --replace it first saves the
current state beside them, so a restore can be undone.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from api import data_controls
from pipeline.runtime_mode import demo_mode


def _root() -> Path:
    root = Path(os.environ.get("ARTICLE_OUTPUT_DIR", "./output"))
    return root / "demo-mode" if demo_mode() else root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.data")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="what is stored, and where")
    backup = commands.add_parser("backup", help="write every record to a zip")
    backup.add_argument("file")
    restore = commands.add_parser("restore", help="put a backup's records back")
    restore.add_argument("file")
    restore.add_argument("--replace", action="store_true",
                         help="overwrite records that already exist")
    args = parser.parse_args(argv)
    root = _root()

    if args.command == "show":
        for kind, found in data_controls.inventory(root, Path(".cache/article_pipeline")).items():
            print(f"  {kind:<12} {found['count']:>5}  {found['bytes'] / 1e6:8.2f} MB  {found['folder']}")
        return 0
    if args.command == "backup":
        target = Path(args.file)
        if target.exists():
            print(f"{target} already exists. Choose another name.", file=sys.stderr)
            return 1
        target.write_bytes(data_controls.export_all(root))
        os.chmod(target, 0o600)
        print(f"Wrote {target} ({target.stat().st_size / 1e6:.2f} MB). It contains your "
              "resumes and interview answers: keep it somewhere private.")
        return 0
    try:
        result = data_controls.restore(root, Path(args.file).read_bytes(),
                                       replace=args.replace)
    except (OSError, data_controls.RestoreRefused) as exc:
        print(f"Not restored: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {result['restored']} file(s); left {result['skipped_existing']} "
          "that already existed.")
    if result["saved_first"]:
        print(f"What was there before is in {result['saved_first']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
