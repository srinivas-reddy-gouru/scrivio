import logging
import os
import re
import subprocess
import uuid
from pathlib import Path

from pipeline.model_config import get_model
from pipeline.schemas.models import RenderAsset, VisualIntent


_SYSTEM_PROMPT = (
    Path(__file__).resolve().parents[1] / "pipeline" / "prompts" / "spec_generator_v1.txt"
).read_text(encoding="utf-8")


# Characters that break Mermaid's flowchart parser when present in a node
# label that isn't wrapped in double quotes. We focus on `(` and `)` because
# the parser interprets them as shape delimiters (rounded nodes) — labels
# like `[postProcessBeforeInitialization()]` therefore fail until quoted.
_PROBLEMATIC_LABEL_CHARS = re.compile(r"[()]")

# Shape patterns where the surrounding bracket characters are NOT parens.
# These can be regex-matched safely because the captured label content
# can't ambiguously contain the outer bracket. Order matters: longer
# (more-bracket) shapes first so `[[label]]` isn't misread as `[label`.
_SHAPE_PATTERNS = [
    (re.compile(r"\[\[([^\[\]]+)\]\]"), "[[", "]]"),  # subroutine
    (re.compile(r"\{\{([^\{\}]+)\}\}"), "{{", "}}"),  # hexagon
    (re.compile(r"\[([^\[\]]+)\]"), "[", "]"),          # rectangle
    (re.compile(r"\{([^\{\}]+)\}"), "{", "}"),          # rhombus
]

# Edge label pattern: `A -->|label| B` (also `---|label|`, `==>|label|`,
# `-.->|label|`, etc.). In flowchart syntax, `|` is exclusively the edge-
# label delimiter, so any `|...|` pair is safe to inspect. The `*` (not `+`)
# also matches the empty case `||` — Mermaid rejects empty edge labels with
# a parse error, so we strip them.
_EDGE_LABEL_PATTERN = re.compile(r"\|([^|]*)\|")


class RenderError(Exception):
    pass


def sanitize_mermaid_spec(spec: str) -> str:
    """Make Mermaid specs robust to common LLM-generated syntax mistakes.

    Two distinct fixes, dispatched by diagram type:

    1. FLOWCHART / GRAPH: ADD quotes around node and edge labels that
       contain parens. Mermaid's flowchart parser treats `()` as shape
       syntax, so an unquoted label like `[postProcessBeforeInitialization()]`
       is rejected. We wrap such labels in double quotes.

    2. STATE DIAGRAM (`stateDiagram-v2`): REMOVE quotes around state IDs.
       Mermaid's state diagram grammar requires state IDs to be bare
       identifiers (`Expecting 'ID', got 'STRING'` is the error). The LLM
       occasionally over-applies the flowchart quoting rule and produces
       `[*] --> "STOPPED"`. We strip the quotes and replace any embedded
       spaces with underscores so the result is a valid identifier.

    Other diagram types (sequence, class, ER) are left untouched — applying
    either transform would corrupt them (classDiagram uses `{}` for class
    bodies, sequenceDiagram has its own message syntax).
    """
    stripped = spec.strip()
    if not stripped:
        return spec

    first_token = stripped.splitlines()[0].strip().lower()

    if first_token.startswith("flowchart") or first_token.startswith("graph"):
        return _sanitize_flowchart(spec)
    if first_token.startswith("classdiagram"):
        return _sanitize_class_diagram(spec)
    if first_token.startswith("statediagram"):
        return _sanitize_state_diagram(spec)
    return spec


def _sanitize_flowchart(spec: str) -> str:
    """Quote flowchart labels containing problematic punctuation, AND
    normalise whitespace inside label brackets/pipes.

    Whitespace handling: Mermaid rejects `| "label" |` (spaces between the
    delimiter and the content) with the same parser error as an unquoted
    label with parens. The fix is to always strip leading/trailing
    whitespace from the captured label region — we re-emit `|"label"|`
    or `|label|` tight, regardless of what the LLM produced.
    """
    # Node labels: `A[label]`, `B((label))`, etc.
    for pattern, open_d, close_d in _SHAPE_PATTERNS:
        def repl(m: re.Match, o: str = open_d, c: str = close_d) -> str:
            label = m.group(1).strip()
            # Already double-quoted (with possible internal padding stripped).
            if label.startswith('"') and label.endswith('"'):
                return f'{o}{label}{c}'
            if _PROBLEMATIC_LABEL_CHARS.search(label):
                return f'{o}"{label}"{c}'
            return f'{o}{label}{c}'
        spec = pattern.sub(repl, spec)

    # Edge labels: `A -->|label| B`. Same handling — but ALWAYS re-emit
    # tight (no spaces inside the pipes) because Mermaid is stricter about
    # whitespace here than inside `[]` brackets.
    #
    # Also handle the empty-edge-label case: `|""|` or `||` produces a
    # parse error ("Expecting TEXT, got PIPE"). The LLM emits these when
    # it has nothing meaningful to put on an edge but feels obligated to
    # label every one. Strip the empty label region entirely so the edge
    # renders as a plain unlabeled arrow.
    def edge_repl(m: re.Match) -> str:
        label = m.group(1).strip()
        # Empty label inside the pipes — strip the entire `|...|` block.
        if label in ("", '""', "''"):
            return ""
        if label.startswith('"') and label.endswith('"'):
            # Check the inner content too — `|"  "|` is also "empty".
            inner = label[1:-1].strip()
            if not inner:
                return ""
            return f"|{label}|"
        if _PROBLEMATIC_LABEL_CHARS.search(label):
            return f'|"{label}"|'
        return f"|{label}|"
    return _EDGE_LABEL_PATTERN.sub(edge_repl, spec)


# classDiagram: LLMs frequently confuse sequence-diagram note syntax with
# classDiagram note syntax.  The sequence form …
#   note right of Shape: Some text
#   note left of Shape: Some text
# … is ILLEGAL in classDiagram and produces `Expecting 'STR', got 'ALPHA'`.
# The correct classDiagram form is:
#   note for Shape "Some text"
_CLASSDIAGRAM_NOTE_PATTERN = re.compile(
    r"note\s+(?:right\s+of|left\s+of|over)\s+(\w+)\s*:\s*(.+)",
    re.IGNORECASE,
)

def _sanitize_class_diagram(spec: str) -> str:
    """Fix invalid note syntax in classDiagram specs.

    LLMs frequently write sequence-diagram note syntax inside classDiagram:
      note right of Shape: Compiler enforces exhaustive matching.
      note left of Circle: Some description.
    Both forms are illegal in classDiagram and cause `Expecting 'STR', got 'ALPHA'`.

    The correct classDiagram syntax is:
      note for Shape "Compiler enforces exhaustive matching."

    We also collapse literal \\n sequences in the note text to spaces because
    Mermaid classDiagram notes don't support embedded newlines.
    """
    def repl(m: re.Match) -> str:
        class_name = m.group(1)
        text = m.group(2).strip()
        # Replace escaped newline markers the LLM may have embedded.
        text = text.replace("\\n", " ")
        # Strip any surrounding quotes the LLM already added.
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1].strip()
        return f'note for {class_name} "{text}"'

    return _CLASSDIAGRAM_NOTE_PATTERN.sub(repl, spec)


# stateDiagram-v2: a quoted state ID like `"STOPPED"` must become a bare
# identifier `STOPPED`. We also collapse interior whitespace to underscores
# so that LLM-generated `"long state name"` becomes `long_state_name` —
# a legal Mermaid state ID rather than a parse error.
_STATE_QUOTED_ID_PATTERN = re.compile(r'"([^"\n]+)"')


def _sanitize_state_diagram(spec: str) -> str:
    """Strip erroneous quoting from state IDs in stateDiagram-v2 specs.

    We only touch quoted strings that appear as state IDs (i.e. before the
    `:` that separates ID from transition label). To stay conservative,
    we process line by line and only de-quote tokens that look like state
    references — adjacent to transition arrows `-->`, the special `[*]`
    marker, or sitting at the start of a state declaration.
    """
    out_lines: list[str] = []
    for line in spec.splitlines():
        # Skip transition labels: everything after the first `:` is free
        # text in stateDiagram and may legitimately contain quoted strings.
        if ":" in line and "-->" in line.split(":", 1)[0]:
            head, tail = line.split(":", 1)
            head = _dequote_state_ids(head)
            out_lines.append(f"{head}:{tail}")
        else:
            out_lines.append(_dequote_state_ids(line))
    return "\n".join(out_lines)


def _dequote_state_ids(text: str) -> str:
    """Replace `"name with spaces"` with `name_with_spaces` in state IDs.

    Preserves the `state "Long Name" as ID` declaration syntax — that
    construct is legal and the quotes are part of the display-label feature
    of stateDiagram-v2. We detect it by looking for `state ` before the
    quoted string.
    """
    def repl(m: re.Match) -> str:
        # Skip the `state "Long Name" as Foo` case — the quotes there are
        # legal syntax. We detect it by looking at the 6 chars before the
        # match position.
        start = m.start()
        preceding = text[max(0, start - 6):start].lower()
        if preceding.endswith("state "):
            return m.group(0)
        # Normalize: collapse whitespace runs to a single underscore,
        # strip leading/trailing whitespace.
        bare = re.sub(r"\s+", "_", m.group(1).strip())
        return bare

    return _STATE_QUOTED_ID_PATTERN.sub(repl, text)


async def generate_spec(intent: VisualIntent, client, preset: str = "balanced") -> str:
    """Generate a Mermaid (or VHS tape) spec for *intent*.

    Accepts any client that exposes the Anthropic ``messages.create()``
    interface — either ``anthropic.AsyncAnthropic`` directly, or the
    ``OpenAIAnthropicAdapter`` shim that wraps an OpenAI client.
    """
    response = await client.messages.create(
        model=get_model("diagram", preset),
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": intent.model_dump_json()}],
    )
    spec = next((b.text for b in response.content if b.type == "text"), "") or ""
    spec = _strip_markdown_fences(spec)
    # Defensive sanitization: even if the prompt tells the LLM to quote
    # labels with parens, occasional misses still happen — this catches them.
    return sanitize_mermaid_spec(spec)


async def render_mermaid(
    spec: str, output_dir: str = "/tmp/article_assets"
) -> str:
    asset_id = str(uuid.uuid4())
    input_path = f"/tmp/mmdc_{asset_id}.mmd"
    output_path = f"{output_dir}/{asset_id}.svg"

    try:
        os.makedirs(output_dir, exist_ok=True)
        Path(input_path).write_text(spec, encoding="utf-8")
        result = subprocess.run(
            [
                "npx",
                "-y",
                "@mermaid-js/mermaid-cli",
                "-i",
                input_path,
                "-o",
                output_path,
                "--quiet",
            ],
            capture_output=True,
            timeout=30,
        )

        if result.returncode != 0:
            raise RenderError(result.stderr.decode("utf-8", errors="replace"))

        output = Path(output_path).read_text(encoding="utf-8")
        if "<svg" not in output:
            raise RenderError("Mermaid output did not contain an SVG")

        return output_path
    finally:
        try:
            os.remove(input_path)
        except FileNotFoundError:
            pass


async def process_visual_intent(
    intent: VisualIntent, client, output_dir="/tmp/article_assets", preset: str = "balanced"
) -> RenderAsset:
    spec = await generate_spec(intent, client, preset=preset)

    try:
        output_path = await render_mermaid(spec, output_dir=output_dir)
    except RenderError as exc:
        logging.error("Mermaid render failed: %s", exc)
        return RenderAsset(intent=intent, spec=spec, output_path="")

    return RenderAsset(intent=intent, spec=spec, output_path=output_path, qa_passed=True)


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines).strip()
