"""Reading and writing the settings file, safely.

The settings file is a .env: provider keys, model choices, a few
switches. The server reads it at startup and the settings screen edits
it. Three things were wrong with how it was written:

  injection   A value was written as KEY=value with no checking, so a
              value containing a newline wrote a second assignment. A
              model name could set a provider key.
  round trip  Values were written unquoted and read back by a different
              parser than the one used at startup. A value containing
              "#" was truncated at the next restart, and "${NAME}" was
              replaced with the contents of another variable.
  durability  The file was truncated and rewritten in place, world
              readable, with nothing to stop two saves interleaving.

Here a value is validated before anything is touched, written in the
one quoting style that reads back literally, and the file is replaced
atomically with owner-only permissions under a lock.

Error messages name the setting and the rule it broke. They never
contain the value: the value is usually a secret, and an error message
is the text most likely to be logged or pasted into a bug report.
"""
from __future__ import annotations

import io
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream

try:                                    # advisory file locks are POSIX
    import fcntl
except ImportError:                     # pragma: no cover
    fcntl = None

MAX_VALUE_LENGTH = 1024
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# C0 controls, DEL, C1 controls, and the Unicode line and paragraph
# separators, which some parsers treat as line breaks.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_SECRET_RE = re.compile(r"^[\x21-\x7e]{8,512}$")       # printable, no spaces
_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")

_process_lock = threading.Lock()


class SettingsError(ValueError):
    """A setting that cannot be saved. `key` is safe to show; the value
    that was rejected is deliberately not kept anywhere on the error."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"{key}: {reason}")
        self.key, self.reason = key, reason


def check_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise SettingsError("(setting name)", "is not a valid setting name")
    return key


def check_value(key: str, value: str, *, kind: str = "text",
                choices: tuple[str, ...] = ()) -> str:
    """The value as it will be stored, or SettingsError.

    kind: secret | model | boolean | choice | text"""
    if not isinstance(value, str):
        raise SettingsError(key, "must be text")
    if _CONTROL_RE.search(value):
        raise SettingsError(key, "contains a line break or control character")
    value = value.strip()
    if len(value) > MAX_VALUE_LENGTH:
        raise SettingsError(key, f"is longer than {MAX_VALUE_LENGTH} characters")
    if not value:
        return ""                                   # empty means "clear"
    if kind == "boolean":
        lowered = value.lower()
        if lowered in _TRUE:
            return "true"
        if lowered in _FALSE:
            return "false"
        raise SettingsError(key, "must be true or false")
    if kind == "choice":
        if value.lower() not in choices:
            raise SettingsError(key, "must be one of: " + ", ".join(choices))
        return value.lower()
    if kind == "model" and not _MODEL_RE.match(value):
        raise SettingsError(
            key, "is not a model name (letters, digits, and . _ : / @ + - only)")
    if kind == "secret" and not _SECRET_RE.match(value):
        raise SettingsError(
            key, "does not look like a key (8 to 512 visible characters, no spaces)")
    return value


def quote(value: str) -> str:
    """Single quotes: the one style the parser reads back literally.

    Inside them only the backslash and the quote itself are special, so
    "#" does not start a comment and a space does not end the value.
    Expansion of ${NAME} is a separate matter, switched off where the
    file is read."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def read(path: Path) -> dict[str, str]:
    """Values exactly as written. No expansion: a secret that happens to
    contain "${" is a secret, not a reference to another variable."""
    if not path.is_file():
        return {}
    values = dotenv_values(path, interpolate=False, encoding="utf-8")
    return {k: v for k, v in values.items() if k and v is not None}


@contextmanager
def _locked(path: Path):
    """One writer at a time: across threads, and across processes that
    share the file."""
    lock_path = path.with_name(path.name + ".lock")
    with _process_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def update(path: Path, changes: dict[str, str], managed: set[str]) -> None:
    """Apply `changes` to the file. An empty value removes the setting.

    Callers validate first; this checks again, because it is the last
    thing between a string and the file. Lines for settings this
    application does not manage are copied through byte for byte,
    including values that span several lines.

    The new file is written beside the old one and moved into place, so
    a reader sees the old file or the new one and never half of either,
    and a failure part way leaves the old file untouched."""
    for key, value in changes.items():
        check_key(key)
        if _CONTROL_RE.search(value):
            raise SettingsError(key, "contains a line break or control character")
    with _locked(path):
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        lines: list[str] = []
        written: set[str] = set()
        for binding in parse_stream(io.StringIO(existing)):
            original = binding.original.string
            key = binding.key
            if binding.error or key is None or key not in managed | set(changes):
                lines.append(original)
                continue
            if key in changes:
                if changes[key] and key not in written:
                    lines.append(f"{key}={quote(changes[key])}\n")
                    written.add(key)
                continue                    # cleared, or a duplicate line
            lines.append(original)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        for key, value in changes.items():
            if value and key not in written:
                lines.append(f"{key}={quote(value)}\n")

        fd, temp = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise
