"""Where the settings file is, and where the data is. One answer.

The server read its settings file when it was imported and then chose
its data folder. The backup tool chose a data folder without reading
the settings file at all. With ARTICLE_OUTPUT_DIR set in that file, the
server kept its records in one folder and the backup tool made a
well-formed backup of another. The setup check and the article command
each had a third and fourth way of doing the same thing.

Everything that needs to know now asks here. Importing this module does
nothing. `load()` reads the settings file into the environment, and is
called by a program when it starts, not by an import.

Precedence is what the server's has always been: a value in the
settings file wins over the same value exported in the shell. That is
the order in which Settings in the interface can be trusted to have
taken effect.

A relative ARTICLE_OUTPUT_DIR, including the default `./output`, is
relative to the directory the program is started from. That is
unchanged, because changing it would move where existing installs look
for their records. It does mean two programs started from two
directories see two folders, so every tool prints the full path it
resolved before it reads or writes anything.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from pipeline.runtime_mode import demo_mode

REPO = Path(__file__).resolve().parent.parent
OUTPUT_SETTING = "ARTICLE_OUTPUT_DIR"
DEFAULT_OUTPUT = "./output"
DEMO_FOLDER = "demo-mode"


def settings_file() -> Path:
    return Path(os.environ.get("SCRIVIO_ENV_FILE") or REPO / ".env")


def _in_file(path: Path) -> dict[str, str | None]:
    try:
        from dotenv import dotenv_values
    except ModuleNotFoundError:
        return {}
    if not path.is_file():
        return {}
    return dict(dotenv_values(path, interpolate=False))


def load() -> Path:
    """Read the settings file into the environment. Returns which file.

    interpolate=False: a key that happens to contain "${" is a key, not a
    reference to another variable."""
    path = settings_file()
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return path
    load_dotenv(path, override=True, interpolate=False)
    return path


def output_root(demo: bool | None = None) -> Path:
    """The folder records are kept in. Demo work has one of its own, so a
    canned review never turns up in the history of a real job search."""
    root = Path(os.environ.get(OUTPUT_SETTING) or DEFAULT_OUTPUT)
    return root / DEMO_FOLDER if (demo_mode() if demo is None else demo) else root


class Resolved(BaseModel):
    """What was decided and why, for printing before anything is touched."""
    settings_file: Path
    settings_found: bool
    output_setting: str
    output_from: Literal["the settings file", "the environment", "the default"]
    demo: bool
    data_root: Path                  # absolute
    started_in: Path

    def lines(self) -> list[str]:
        found = "" if self.settings_found else " (not there, so nothing was read from it)"
        return [
            f"Settings file  {self.settings_file}{found}",
            f"Data folder    {self.data_root}",
            f"               from {OUTPUT_SETTING}={self.output_setting!r}, set by "
            f"{self.output_from}" + (", in demo mode" if self.demo else ""),
        ]


def resolve() -> Resolved:
    """Call after load()."""
    path = settings_file()
    in_file = _in_file(path)
    if in_file.get(OUTPUT_SETTING):
        source = "the settings file"
    elif os.environ.get(OUTPUT_SETTING):
        source = "the environment"
    else:
        source = "the default"
    return Resolved(
        settings_file=path, settings_found=path.is_file(),
        output_setting=os.environ.get(OUTPUT_SETTING) or DEFAULT_OUTPUT,
        output_from=source, demo=demo_mode(),
        data_root=output_root().resolve(), started_in=Path.cwd())
