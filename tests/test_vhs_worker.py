import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from render import vhs_worker
from render.mermaid_worker import RenderError
from render.vhs_worker import render_vhs


def test_render_vhs_returns_path_when_gif_created(monkeypatch, tmp_path) -> None:
    asset_id = "00000000-0000-0000-0000-000000000101"
    output_dir = tmp_path / "assets"
    expected_gif = output_dir / f"{asset_id}.gif"
    tape_path = Path(f"/tmp/vhs_{asset_id}.tape")

    def fake_run(command, capture_output, timeout):
        assert command == ["vhs", str(tape_path)]
        expected_gif.write_bytes(b"0" * 2000)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(vhs_worker.uuid, "uuid4", lambda: asset_id)
    monkeypatch.setattr(vhs_worker.subprocess, "run", fake_run)

    gif_path = asyncio.run(
        render_vhs("Set FontSize 18\nType \"hello\"", output_dir=str(output_dir))
    )

    assert gif_path == str(expected_gif)
    assert expected_gif.stat().st_size == 2000
    assert not tape_path.exists()


def test_render_vhs_raises_render_error_on_subprocess_failure(
    monkeypatch, tmp_path
) -> None:
    asset_id = "00000000-0000-0000-0000-000000000102"

    def fake_run(command, capture_output, timeout):
        return SimpleNamespace(returncode=1, stderr=b"vhs failed")

    monkeypatch.setattr(vhs_worker.uuid, "uuid4", lambda: asset_id)
    monkeypatch.setattr(vhs_worker.subprocess, "run", fake_run)

    with pytest.raises(RenderError, match="vhs failed"):
        asyncio.run(render_vhs("Type \"hello\"", output_dir=str(tmp_path)))


def test_render_vhs_injects_output_directive_on_first_line(
    monkeypatch, tmp_path
) -> None:
    asset_id = "00000000-0000-0000-0000-000000000103"
    expected_gif = tmp_path / f"{asset_id}.gif"
    observed_first_line = []

    def fake_run(command, capture_output, timeout):
        tape_content = Path(command[1]).read_text(encoding="utf-8")
        observed_first_line.append(tape_content.splitlines()[0])
        expected_gif.write_bytes(b"0" * 2000)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(vhs_worker.uuid, "uuid4", lambda: asset_id)
    monkeypatch.setattr(vhs_worker.subprocess, "run", fake_run)

    asyncio.run(render_vhs("Set FontSize 18\nType \"hello\"", output_dir=str(tmp_path)))

    assert observed_first_line == [f"Output {expected_gif}"]
