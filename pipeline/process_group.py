"""Stopping a child process and everything it started.

A program that is started in a process group of its own can be stopped
together with its children by signalling the group. The group has a
number, and the number is the child's pid at the moment it was started.

That number has to be kept. The first version asked the operating system
for it at the end, by looking up the group of the child's pid, and the
lookup fails once the child has exited and been collected. So a renderer
that started a browser and then finished cleanly left the browser
running: the one case in which there was something to clean up and
nothing in the way of doing it.

What this cannot reach: a descendant that starts a session or a group of
its own has left, and signalling the group it left does not find it.
Containing a program that does that on purpose takes the operating
system: a container, a job object, a cgroup.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal


def kill_group(group: int | None) -> None:
    """SIGKILL to every process still in `group`. Quiet when there are
    none, which is the ordinary case after a clean exit."""
    if not group or group <= 1:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(group, signal.SIGKILL)


async def exit_code(process, every: float = 0.02) -> int:
    """The child's exit code, as soon as the child has exited.

    Not process.wait(). That does not return until the child's pipes
    have closed, and the pipes close when the LAST process holding them
    exits. A child that started something and left it running has
    exited, and wait() goes on waiting for what it left."""
    while process.returncode is None:
        await asyncio.sleep(every)
    return process.returncode


async def reap(process, seconds: float = 5.0) -> None:
    """Collect the child, so that it does not stay behind as a zombie."""
    if process.returncode is not None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=seconds)


async def settle(*tasks) -> None:
    """Cancel what is still running and wait until it has stopped. A
    task left reading a pipe is reported by the event loop when it
    closes, and holds the pipe until then."""
    pending = [t for t in tasks if t is not None and not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in tasks:
        if task is not None and task.done() and not task.cancelled():
            task.exception()             # looked at, so it is not reported as unretrieved
