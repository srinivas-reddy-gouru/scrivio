import logging
import os
import subprocess
import uuid
from pathlib import Path

from pipeline.schemas.models import RenderAsset, VisualIntent
from render.mermaid_worker import RenderError, generate_spec


async def render_vhs(
    tape_script: str, output_dir: str = "/tmp/article_assets"
) -> str:
    asset_id = str(uuid.uuid4())
    tape_path = f"/tmp/vhs_{asset_id}.tape"
    gif_path = f"{output_dir}/{asset_id}.gif"
    full_script = f"Output {gif_path}\n{tape_script}"

    try:
        os.makedirs(output_dir, exist_ok=True)
        Path(tape_path).write_text(full_script, encoding="utf-8")
        result = subprocess.run(
            ["vhs", tape_path],
            capture_output=True,
            timeout=60,
        )

        if result.returncode != 0:
            raise RenderError(result.stderr.decode("utf-8", errors="replace"))

        if os.path.getsize(gif_path) <= 1000:
            raise RenderError("VHS output GIF was too small")

        return gif_path
    finally:
        try:
            os.remove(tape_path)
        except FileNotFoundError:
            pass


async def process_vhs_intent(
    intent: VisualIntent, client, output_dir="/tmp/article_assets", preset: str = "balanced"
) -> RenderAsset:
    spec = await generate_spec(intent, client, preset=preset)

    try:
        output_path = await render_vhs(spec, output_dir=output_dir)
    except RenderError as exc:
        logging.error("VHS render failed: %s", exc)
        return RenderAsset(intent=intent, spec=spec, output_path="")

    return RenderAsset(intent=intent, spec=spec, output_path=output_path, qa_passed=True)
