"""Running a renderer as a child process, with an end.

The renderers used subprocess.run() inside async functions. That blocks
the event loop for the length of the render, so every other request
waits behind it, and it cannot be cancelled: a job that was cancelled
went on rendering. On a timeout it killed the process it started and
left that process's own children running, which for a diagram render is
a headless browser.

run_bounded() is the replacement:

  off the loop   an asyncio subprocess, so other requests keep moving
  a deadline     and the whole process GROUP is killed when it passes,
                 so nothing the renderer started outlives it
  cancellable    cancelling the caller kills the group too
  bounded output read with a ceiling, not buffered whole
  a clean env    the child gets a short list of variables. It does not
                 inherit the server's environment, which holds provider
                 keys the renderer has no use for.
"""
from __future__ import annotations

import asyncio
import os
import signal

MAX_OUTPUT_BYTES = 2_000_000
_PASSED_THROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM",
                   "PUPPETEER_EXECUTABLE_PATH", "PUPPETEER_CACHE_DIR")


class ProcessFailed(Exception):
    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


class ToolMissing(ProcessFailed):
    """The program is not installed. Not a render failure: nothing ran."""


def clean_environment() -> dict[str, str]:
    return {k: os.environ[k] for k in _PASSED_THROUGH if k in os.environ}


def _kill_group(process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


class _Overrun(Exception):
    pass


async def _read(stream, limit: int) -> bytes:
    """Raises rather than returning early. A reader that simply stopped
    would leave the other stream being waited on, and the child blocked
    writing into a pipe nobody is emptying, until the timeout."""
    held = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(held)
        held.extend(chunk)
        if len(held) > limit:
            raise _Overrun


async def run_bounded(
    argv: list[str], *, timeout: float, cwd: str | None = None,
    max_output: int = MAX_OUTPUT_BYTES,
) -> tuple[int, bytes, bytes]:
    """(return code, stdout, stderr), or ProcessFailed."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_environment(), cwd=cwd,
            start_new_session=True,          # its own group, so it can be
        )                                    # killed together with its children
    except FileNotFoundError as exc:
        raise ToolMissing(f"{argv[0]} is not installed") from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            asyncio.gather(_read(process.stdout, max_output),
                           _read(process.stderr, max_output)),
            timeout=timeout,
        )
        code = await asyncio.wait_for(process.wait(), timeout=timeout)
        return code, stdout, stderr
    except _Overrun:
        raise ProcessFailed(
            f"{argv[0]} produced more output than the limit and was stopped"
        ) from None
    except asyncio.TimeoutError:
        raise ProcessFailed(
            f"{argv[0]} did not finish within {timeout:g} seconds and was stopped",
            timed_out=True) from None
    finally:
        # Reached on success too, where it finds nothing left to kill. On a
        # timeout, an overrun, or a cancelled caller it is the point.
        if process.returncode is None:
            _kill_group(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass
        else:
            _kill_group(process)             # children of a finished parent
