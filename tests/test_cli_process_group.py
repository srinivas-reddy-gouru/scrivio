"""An abandoned call to a command-line assistant leaves nothing running
(F05, the same fault in a second place).

The follow-up review found it in the renderer and asked for the
assistant's runner to be checked for the same pattern. It was worse
there: the runner stopped the process it had started and nothing else,
with no group at all, so whatever an assistant had started went on
running after a timeout or a cancelled job.

The assistant here is a small program written for the test.
"""
import asyncio
import os
import stat
import sys
import time

import pytest

from pipeline.providers import claude_cli_adapter as adapter


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    """Starts a helper, says who it and the helper are, and then does
    what `behaviour` says: hang, flood, or answer."""
    pids = tmp_path / "pids"
    program = tmp_path / "claude"
    program.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{helper.pid}}")
sys.stdin.read()
behaviour = os.environ.get("SCRIVIO_TEST_ASSISTANT", "answer")
if behaviour == "hang":
    time.sleep(120)
if behaviour == "flood":
    while True:
        sys.stdout.write("x" * 65536)
print(json.dumps({{"result": "done", "is_error": False}}))
""")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("LLM_CLI", "claude")
    monkeypatch.setattr(adapter, "_find_cli", lambda: str(program))
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)

    def started() -> tuple[int, int]:
        for _ in range(100):
            if pids.exists() and len(pids.read_text().split()) == 2:
                return tuple(int(p) for p in pids.read_text().split())
            time.sleep(0.05)
        raise AssertionError("the assistant never started")

    yield started
    if pids.exists():
        for pid in pids.read_text().split():
            try:
                os.kill(int(pid), 9)
            except (ProcessLookupError, ValueError):
                pass


def call():
    return adapter._CLIMessages()._run_cli("sonnet", "hello")


def test_a_call_that_times_out_stops_the_assistant_and_what_it_started(assistant, monkeypatch):
    monkeypatch.setenv("SCRIVIO_TEST_ASSISTANT", "hang")
    monkeypatch.setattr(adapter, "_CALL_TIMEOUT_S", 1.5)

    with pytest.raises(adapter.ClaudeCLIError, match="timed out"):
        asyncio.run(call())

    parent, helper = assistant()
    assert wait_gone(parent), "the assistant survived its timeout"
    assert wait_gone(helper), "what the assistant started survived it"


def test_a_cancelled_job_stops_the_assistant_and_what_it_started(assistant, monkeypatch):
    monkeypatch.setenv("SCRIVIO_TEST_ASSISTANT", "hang")

    async def scenario():
        running = asyncio.create_task(call())
        await asyncio.to_thread(assistant)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(scenario())

    parent, helper = assistant()
    assert wait_gone(parent) and wait_gone(helper)


def test_too_much_output_stops_the_assistant_and_what_it_started(assistant, monkeypatch):
    monkeypatch.setenv("SCRIVIO_TEST_ASSISTANT", "flood")
    monkeypatch.setattr(adapter, "MAX_OUTPUT_BYTES", 200_000)

    with pytest.raises(adapter.ClaudeCLIError, match="more output"):
        asyncio.run(call())

    parent, helper = assistant()
    assert wait_gone(parent) and wait_gone(helper)


def test_a_call_that_succeeds_leaves_the_assistants_helper_alone(assistant):
    """On purpose. An assistant may keep something running between calls,
    a local model server for instance, and stopping it after every
    answer would make the next one start from nothing."""
    asyncio.run(call())

    _parent, helper = assistant()
    assert alive(helper)


def test_the_assistant_is_given_a_group_of_its_own_and_not_a_session(assistant, monkeypatch):
    monkeypatch.setenv("SCRIVIO_TEST_ASSISTANT", "hang")
    seen = {}

    async def scenario():
        running = asyncio.create_task(call())
        parent, _helper = await asyncio.to_thread(assistant)
        seen["group"] = os.getpgid(parent)
        seen["session"] = os.getsid(parent)
        seen["parent"] = parent
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(scenario())

    assert seen["group"] == seen["parent"], "it leads a group of its own"
    assert seen["session"] == os.getsid(0), "and is still in the session it was started from"
