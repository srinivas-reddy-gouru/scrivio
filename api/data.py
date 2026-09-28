"""Back up and restore from the command line.

    python -m api.data where
    python -m api.data show
    python -m api.data backup  scrivio-backup.zip
    python -m api.data restore scrivio-backup.zip [--replace]

The server does not need to be running. Restore leaves existing records
alone unless --replace is given, and with --replace it first saves the
current state beside them, so a restore can be undone.

Every command reads the same settings file the server reads, and prints
the folder it resolved before it reads or writes anything. It did not
always: it once chose its folder without reading the settings file, and
backed up `./output` while the server kept its records somewhere else.
A backup of the wrong folder is a well-formed zip, so nothing looked
wrong until it was needed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from api import data_controls
from pipeline import local_settings

# What the inventory counts that is not a record of the user's.
_NOT_RECORDS = ("stage_cache", "set_aside")


def records_in(root: Path) -> int:
    found = data_controls.inventory(root, Path(".cache/article_pipeline"))
    return sum(kind["count"] for name, kind in found.items() if name not in _NOT_RECORDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.data")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("where", help="which settings file and data folder are in use")
    commands.add_parser("show", help="what is stored, and where")
    backup = commands.add_parser("backup", help="write every record to a zip")
    backup.add_argument("file")
    backup.add_argument("--allow-empty", action="store_true",
                        help="write the backup even if the folder holds no records")
    restore = commands.add_parser("restore", help="put a backup's records back")
    restore.add_argument("file")
    restore.add_argument("--replace", action="store_true",
                         help="overwrite records that already exist")
    args = parser.parse_args(argv)

    local_settings.load()
    resolved = local_settings.resolve()
    root = resolved.data_root
    for line in resolved.lines():
        print(line)
    if args.command == "where":
        print(f"Records        {records_in(root)}")
        return 0
    print()

    if args.command == "show":
        for kind, found in data_controls.inventory(root, Path(".cache/article_pipeline")).items():
            print(f"  {kind:<12} {found['count']:>5}  {found['bytes'] / 1e6:8.2f} MB  {found['folder']}")
        return 0

    if args.command == "backup":
        target = Path(args.file)
        if target.exists():
            print(f"{target} already exists. Choose another name.", file=sys.stderr)
            return 1
        count = records_in(root)
        if count == 0 and not args.allow_empty:
            print(
                f"Nothing was backed up: there are no records in {root}.\n"
                "If you have used Scrivio, this is not the folder it keeps them in. "
                "Check the settings file and the directory this was started from, "
                "both printed above. To write an empty backup anyway, add --allow-empty.",
                file=sys.stderr)
            return 1
        target.write_bytes(data_controls.export_all(root))
        os.chmod(target, 0o600)
        print(f"Backed up {count} record(s) from {root}")
        print(f"Wrote {target.resolve()} ({target.stat().st_size / 1e6:.2f} MB). It contains "
              "your resumes and interview answers: keep it somewhere private.")
        return 0

    print(f"Restoring into {root}")
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
