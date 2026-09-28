"""Terminal recordings (VHS). Disabled.

A VHS tape is a script of keystrokes typed into a real shell. The tape is
written by a model, the model has been reading the open web, and the
shell is on the machine that holds the user's resumes and provider keys.
Rendering a tape on the host is running commands chosen by whoever wrote
the pages the model read.

So there is no host renderer. render_vhs() runs a tape only through a
`runner` that the caller supplies, and nothing in this codebase supplies
one. There is no setting that turns host execution back on: a command
denylist is not isolation, and a switch that says "unsafe" gets switched.

What enabling this properly needs, none of which exists yet:
  - a container or VM with no credentials in it
  - no network, or an allow list
  - a filesystem that is thrown away after the render
  - limits on CPU, memory, processes, and time
  - tests that show a hostile tape cannot reach the host
"""
import logging
import os
import tempfile
from pathlib import Path

from pipeline.schemas.models import RenderAsset, VisualIntent
from render.mermaid_worker import RenderError, generate_spec


class RenderDisabled(RenderError):
    """The renderer exists and is switched off on purpose."""


DISABLED_REASON = (
    "Terminal recordings are disabled. A recording is made by typing "
    "model-written commands into a real shell, and this release has no "
    "isolated environment to do that in."
)


async def render_vhs(
    tape_script: str, output_dir: str = "/tmp/article_assets", *, runner=None,
) -> str:
    """Render a tape with `runner`, an isolated executor:

        async def runner(tape_path: str, output_path: str) -> None

    Without one this raises RenderDisabled and executes nothing."""
    if runner is None:
        raise RenderDisabled(DISABLED_REASON)

    os.makedirs(output_dir, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="scrivio-vhs-")
    tape_path = os.path.join(workdir, "render.tape")
    gif_path = os.path.join(output_dir, f"{os.path.basename(workdir)}.gif")
    try:
        Path(tape_path).write_text(
            f"Output {gif_path}\n{tape_script}", encoding="utf-8")
        await runner(tape_path, gif_path)
        if not os.path.isfile(gif_path) or os.path.getsize(gif_path) <= 1000:
            raise RenderError("The recording that came back was empty")
        return gif_path
    finally:
        try:
            os.remove(tape_path)
            os.rmdir(workdir)
        except OSError:
            pass


async def process_vhs_intent(
    intent: VisualIntent, client, output_dir="/tmp/article_assets",
    preset: str = "balanced", *, runner=None,
) -> RenderAsset:
    """With no runner, which is always for now, no tape is even generated:
    asking a model to write a script nobody will run is a paid call for
    nothing."""
    if runner is None:
        logging.info("Skipped a terminal recording: %s", DISABLED_REASON)
        return RenderAsset(intent=intent, spec="", output_path="")

    spec = await generate_spec(intent, client, preset=preset)
    try:
        output_path = await render_vhs(spec, output_dir=output_dir, runner=runner)
    except RenderError as exc:
        logging.error("VHS render failed: %s", exc)
        return RenderAsset(intent=intent, spec=spec, output_path="")
    return RenderAsset(intent=intent, spec=spec, output_path=output_path, qa_passed=True)
