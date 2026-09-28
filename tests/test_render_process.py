"""The renderer process runner (R12): bounded, cancellable, and thorough
about what it leaves behind.

These tests start real child processes, all of them `python -c ...`,
because whether a grandchild survives a kill is not something a mock can
answer.
"""
import asyncio
import os
import sys
import time

import pytest

from render.process import ProcessFailed, ToolMissing, run_bounded

PY = sys.executable


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


# A parent that starts a child, reports both pids, and then hangs: the
# shape of a diagram renderer that launched a headless browser.
SPAWNS_A_CHILD = (
    "import subprocess, sys, time, os;"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
    "open(sys.argv[1], 'w').write(f'{os.getpid()} {child.pid}');"
    "time.sleep(120)"
)


def test_an_ordinary_command_returns_its_output() -> None:
    code, out, err = asyncio.run(run_bounded(
        [PY, "-c", "import sys; print('drawn'); print('note', file=sys.stderr)"], timeout=20))

    assert (code, out.strip(), err.strip()) == (0, b"drawn", b"note")


def test_a_failing_command_reports_its_code_without_raising() -> None:
    code, _out, err = asyncio.run(run_bounded(
        [PY, "-c", "import sys; sys.stderr.write('parse error'); sys.exit(3)"], timeout=20))

    assert code == 3 and err == b"parse error"


def test_a_missing_tool_is_reported_as_missing() -> None:
    with pytest.raises(ToolMissing, match="is not installed"):
        asyncio.run(run_bounded(["scrivio-no-such-renderer"], timeout=5))


def test_a_timeout_kills_the_renderer_and_everything_it_started(tmp_path) -> None:
    pids = tmp_path / "pids"

    started = time.monotonic()
    with pytest.raises(ProcessFailed) as failed:
        asyncio.run(run_bounded([PY, "-c", SPAWNS_A_CHILD, str(pids)], timeout=2))

    assert failed.value.timed_out and time.monotonic() - started < 10
    parent, child = (int(p) for p in pids.read_text().split())
    assert wait_gone(parent), "the renderer survived its timeout"
    assert wait_gone(child), "the process the renderer started survived it"


def test_cancelling_the_job_kills_the_renderer_and_everything_it_started(tmp_path) -> None:
    pids = tmp_path / "pids"

    async def scenario():
        render = asyncio.create_task(
            run_bounded([PY, "-c", SPAWNS_A_CHILD, str(pids)], timeout=60))
        for _ in range(100):
            if pids.exists() and pids.read_text().strip():
                break
            await asyncio.sleep(0.05)
        render.cancel()
        with pytest.raises(asyncio.CancelledError):
            await render

    asyncio.run(scenario())

    parent, child = (int(p) for p in pids.read_text().split())
    assert wait_gone(parent) and wait_gone(child)


def test_output_beyond_the_limit_stops_the_command() -> None:
    with pytest.raises(ProcessFailed, match="more output"):
        asyncio.run(run_bounded(
            [PY, "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536)"],
            timeout=20, max_output=200_000))


def test_the_renderer_does_not_inherit_provider_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("SCRIVIO_STATE_DIR", "/somewhere/private")

    _code, out, _err = asyncio.run(run_bounded(
        [PY, "-c", "import os; print(sorted(os.environ))"], timeout=20))

    names = out.decode()
    assert "API_KEY" not in names and "SCRIVIO" not in names
    assert "PATH" in names


def test_other_work_continues_while_a_render_runs() -> None:
    """The point of leaving subprocess.run(): the loop is not held."""
    async def scenario():
        render = asyncio.create_task(run_bounded(
            [PY, "-c", "import time; time.sleep(1.5)"], timeout=20))
        ticks = 0
        while not render.done():
            await asyncio.sleep(0.05)
            ticks += 1
        await render
        return ticks

    assert asyncio.run(scenario()) > 10


# ── F05: the parent finishes first ────────────────────────────────────
# Every test above keeps the renderer alive until it is stopped. A
# renderer that starts a browser and exits cleanly, leaving the browser
# behind, was not covered, and was not cleaned up: the group was looked
# up from the parent's pid, and the parent was gone.

# A parent that starts a child, says who the child is, and exits with
# success. `{pipes}` decides whether the child holds the parent's output
# pipes open or lets go of them.
LEAVES_A_CHILD = (
    "import subprocess, sys, os;"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']{pipes});"
    "open(sys.argv[1], 'w').write(str(child.pid));"
    "print('drawn')"
)
LETS_GO = LEAVES_A_CHILD.format(
    pipes=", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL")
HOLDS_ON = LEAVES_A_CHILD.format(pipes="")


@pytest.fixture
def left_behind(tmp_path):
    """Whatever a test leaves running is stopped here, pass or fail."""
    record = tmp_path / "child"
    yield record
    if record.exists() and record.read_text().strip().isdigit():
        try:
            os.kill(int(record.read_text()), 9)
        except ProcessLookupError:
            pass


def test_the_case_from_the_follow_up_review(left_behind) -> None:
    code, out, _err = asyncio.run(run_bounded([PY, "-c", LETS_GO, str(left_behind)], timeout=20))

    assert code == 0 and out.strip() == b"drawn"
    assert wait_gone(int(left_behind.read_text())), \
        "the renderer finished, and what it started is still running"


def test_a_child_that_holds_the_pipes_open_does_not_hold_up_the_result(left_behind) -> None:
    """The output pipes do not close until the last process holding them
    exits. Waiting for them to close meant waiting for the child, up to
    the full timeout, and then reporting a timeout for a render that had
    finished."""
    started = time.monotonic()

    code, out, _err = asyncio.run(run_bounded([PY, "-c", HOLDS_ON, str(left_behind)], timeout=30))

    assert code == 0 and out.strip() == b"drawn"
    assert time.monotonic() - started < 10
    assert wait_gone(int(left_behind.read_text()))


def test_a_parent_that_fails_after_starting_a_child_leaves_nothing(left_behind) -> None:
    failing = LETS_GO.replace("print('drawn')", "sys.exit(4)")

    code, _out, _err = asyncio.run(run_bounded([PY, "-c", failing, str(left_behind)], timeout=20))

    assert code == 4
    assert wait_gone(int(left_behind.read_text()))


def test_output_beyond_the_limit_leaves_nothing_either(left_behind) -> None:
    noisy = LETS_GO.replace(
        "print('drawn')", "\nwhile True: sys.stdout.write('x' * 65536)")

    with pytest.raises(ProcessFailed, match="more output"):
        asyncio.run(run_bounded([PY, "-c", noisy, str(left_behind)],
                                timeout=20, max_output=200_000))

    assert wait_gone(int(left_behind.read_text()))


@pytest.mark.parametrize("program", [LETS_GO, HOLDS_ON], ids=["lets-go", "holds-on"])
def test_no_task_is_left_waiting_on_a_pipe(left_behind, program) -> None:
    async def scenario():
        before = len(asyncio.all_tasks())
        await run_bounded([PY, "-c", program, str(left_behind)], timeout=20)
        await asyncio.sleep(0)
        return before, len(asyncio.all_tasks())

    before, after = asyncio.run(scenario())

    assert after == before


def test_a_child_that_leaves_the_group_on_purpose_is_out_of_reach(left_behind) -> None:
    """The limit, written down as a test so that it stays true to what
    the documentation says. A process that starts a session of its own
    has left the group, and killing the group does not reach it. The
    renderers used here do not do this. Containing one that did needs
    the operating system: a container, a job object, or a cgroup."""
    escapes = (
        "import subprocess, sys;"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
        " start_new_session=True);"
        "open(sys.argv[1], 'w').write(str(child.pid));"
        "print('drawn')"
    )

    code, _out, _err = asyncio.run(run_bounded([PY, "-c", escapes, str(left_behind)], timeout=20))

    assert code == 0
    assert alive(int(left_behind.read_text()))
