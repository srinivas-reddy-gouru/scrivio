"""Terminal recordings are disabled, and cannot be switched on (R12).

A tape is a script of keystrokes for a real shell, written by a model.
These tests pin the property that matters: with nothing but this
codebase, no tape is ever executed on the host.
"""
import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.schemas.models import VisualIntent
from render import vhs_worker
from render.mermaid_worker import RenderError
from render.vhs_worker import RenderDisabled, process_vhs_intent, render_vhs

HOSTILE_TAPE = 'Type "touch {marker}"\nEnter\nSleep 1s'


@pytest.fixture
def no_process_may_start(monkeypatch):
    """Any attempt to start a process fails the test, by any route."""
    started = []

    def refuse(*args, **kwargs):
        started.append(args)
        raise AssertionError(f"a process was started: {args[:1]}")

    async def refuse_async(*args, **kwargs):
        refuse(*args)

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_async)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_async)
    return started


def test_a_tape_is_not_executed_by_default(no_process_may_start, tmp_path) -> None:
    marker = tmp_path / "executed"

    with pytest.raises(RenderDisabled, match="disabled"):
        asyncio.run(render_vhs(HOSTILE_TAPE.format(marker=marker), output_dir=str(tmp_path)))

    assert no_process_may_start == []
    assert not marker.exists()


def test_a_disabled_render_is_still_a_render_error() -> None:
    """Callers that already handle RenderError handle this without change."""
    assert issubclass(RenderDisabled, RenderError)


@pytest.mark.parametrize("name", [
    "SCRIVIO_ALLOW_HOST_VHS", "SCRIVIO_UNSAFE_HOST_VHS", "ENABLE_VHS", "VHS_ENABLED",
    "ALLOW_VHS", "SCRIVIO_VHS",
])
def test_no_environment_variable_turns_host_execution_on(
    monkeypatch, no_process_may_start, tmp_path, name
) -> None:
    monkeypatch.setenv(name, "1")

    with pytest.raises(RenderDisabled):
        asyncio.run(render_vhs('Type "id"', output_dir=str(tmp_path)))

    assert no_process_may_start == []


def test_the_pipeline_skips_a_recording_without_paying_for_a_tape(
    no_process_may_start, tmp_path
) -> None:
    """With no way to run it, asking a model to write the tape would be a
    paid call for nothing."""
    calls = []

    class Model:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("the model was asked for a tape")

    intent = VisualIntent(
        description="Show a consumer group rebalancing", format="vhs",
        rationale="Seeing the pause makes it concrete.", section_title="Rebalancing")

    asset = asyncio.run(process_vhs_intent(intent, Model(), output_dir=str(tmp_path)))

    assert calls == [] and no_process_may_start == []
    assert asset.qa_passed is False and asset.output_path == ""


def test_the_host_has_no_renderer_in_the_codebase() -> None:
    """The check a reviewer would do by hand: nothing invokes `vhs`."""
    root = Path(__file__).resolve().parents[1]
    invoking = []
    for path in [*root.glob("render/*.py"), *root.glob("pipeline/**/*.py"),
                 *root.glob("api/*.py"), root / "main.py"]:
        text = path.read_text()
        if '"vhs"' in text and ("subprocess" in text or "create_subprocess" in text):
            if '["vhs"' in text or "'vhs'," in text:
                invoking.append(str(path.relative_to(root)))

    assert invoking == []


# ── With an isolated runner, which does not exist yet ────────────────

def test_a_supplied_runner_is_given_the_tape_and_the_output_path(tmp_path) -> None:
    seen = {}

    async def runner(tape_path: str, output_path: str) -> None:
        seen["tape"] = Path(tape_path).read_text()
        Path(output_path).write_bytes(b"0" * 2000)

    gif = asyncio.run(render_vhs('Set FontSize 18\nType "hello"',
                                 output_dir=str(tmp_path), runner=runner))

    assert seen["tape"].splitlines()[0] == f"Output {gif}"
    assert Path(gif).stat().st_size == 2000


def test_an_empty_recording_from_a_runner_is_a_failure(tmp_path) -> None:
    async def runner(tape_path: str, output_path: str) -> None:
        Path(output_path).write_bytes(b"0" * 10)

    with pytest.raises(RenderError, match="empty"):
        asyncio.run(render_vhs('Type "hello"', output_dir=str(tmp_path), runner=runner))


def test_the_api_says_recordings_are_off_rather_than_dropping_them_quietly() -> None:
    from fastapi.testclient import TestClient

    from api import server

    r = TestClient(server.app).post("/generate", json={
        "topic": "Kafka rebalancing", "must_cover": ["pauses"], "include_gifs": True})

    assert r.status_code == 422
    assert "disabled" in r.json()["detail"]
