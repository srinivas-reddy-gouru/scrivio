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
  after success  the group is killed then as well: a renderer that
                 finishes and leaves a browser running has not cleaned
                 up, and nothing else will
  bounded output read with a ceiling, not buffered whole
  a clean env    the child gets a short list of variables. It does not
                 inherit the server's environment, which holds provider
                 keys the renderer has no use for.
"""
from __future__ import annotations

import asyncio
import os

from pipeline.process_group import exit_code, kill_group, reap, settle

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


# How long output may go on arriving after the program has exited. The
# pipes close when the LAST process holding them exits, so a child the
# program left running keeps them open. Waiting for that is waiting for
# the child.
DRAIN_SECONDS = 0.5


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

    # The group's number is the child's pid, from the moment it starts,
    # because it starts a session of its own. Kept here: by the time it
    # is needed the child may be gone, and there is nobody left to ask.
    group = process.pid
    reading = asyncio.ensure_future(asyncio.gather(
        _read(process.stdout, max_output), _read(process.stderr, max_output)))
    exiting = asyncio.ensure_future(exit_code(process))
    try:
        done, _ = await asyncio.wait(
            {reading, exiting}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise asyncio.TimeoutError
        if reading in done and reading.exception() is not None:
            raise reading.exception()
        if exiting not in done:
            # Output has ended and the program has not. It gets what is
            # left of the time.
            await asyncio.wait({exiting}, timeout=timeout)
            if not exiting.done():
                raise asyncio.TimeoutError
        if not reading.done():
            await asyncio.wait({reading}, timeout=DRAIN_SECONDS)
        if not reading.done():
            # Something the program started is holding the pipes. Stopping
            # it closes them, and what was written before then is kept.
            kill_group(group)
            await asyncio.wait({reading}, timeout=DRAIN_SECONDS)
        if not reading.done():
            raise asyncio.TimeoutError
        stdout, stderr = reading.result()
        return exiting.result(), stdout, stderr
    except _Overrun:
        raise ProcessFailed(
            f"{argv[0]} produced more output than the limit and was stopped"
        ) from None
    except asyncio.TimeoutError:
        raise ProcessFailed(
            f"{argv[0]} did not finish within {timeout:g} seconds and was stopped",
            timed_out=True) from None
    finally:
        # Every way out comes through here: success, failure, a timeout,
        # too much output, and a cancelled caller.
        kill_group(group)
        await settle(reading, exiting)
        await reap(process)
