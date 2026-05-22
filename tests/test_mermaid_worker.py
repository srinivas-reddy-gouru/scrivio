import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from render import mermaid_worker
from render.mermaid_worker import (
    RenderError,
    render_mermaid,
    sanitize_mermaid_spec,
)


def test_render_mermaid_returns_path_when_svg_created(monkeypatch, tmp_path) -> None:
    asset_id = "00000000-0000-0000-0000-000000000001"
    output_dir = tmp_path / "assets"
    expected_output = output_dir / f"{asset_id}.svg"
    input_path = Path(f"/tmp/mmdc_{asset_id}.mmd")

    def fake_run(command, capture_output, timeout):
        Path(command[command.index("-o") + 1]).write_text(
            "<svg></svg>", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(mermaid_worker.uuid, "uuid4", lambda: asset_id)
    monkeypatch.setattr(mermaid_worker.subprocess, "run", fake_run)

    output_path = asyncio.run(
        render_mermaid("flowchart LR\n  A --> B", output_dir=str(output_dir))
    )

    assert output_path == str(expected_output)
    assert expected_output.read_text(encoding="utf-8") == "<svg></svg>"
    assert not input_path.exists()


def test_render_mermaid_raises_render_error_on_subprocess_failure(
    monkeypatch, tmp_path
) -> None:
    asset_id = "00000000-0000-0000-0000-000000000002"

    def fake_run(command, capture_output, timeout):
        return SimpleNamespace(returncode=1, stderr=b"parse error")

    monkeypatch.setattr(mermaid_worker.uuid, "uuid4", lambda: asset_id)
    monkeypatch.setattr(mermaid_worker.subprocess, "run", fake_run)

    with pytest.raises(RenderError, match="parse error"):
        asyncio.run(
            render_mermaid("flowchart LR\n  A -->", output_dir=str(tmp_path))
        )


def test_render_mermaid_cleans_temp_file_on_error(monkeypatch, tmp_path) -> None:
    asset_id = "00000000-0000-0000-0000-000000000003"
    input_path = Path(f"/tmp/mmdc_{asset_id}.mmd")

    def fake_run(command, capture_output, timeout):
        assert input_path.exists()
        return SimpleNamespace(returncode=1, stderr=b"boom")

    monkeypatch.setattr(mermaid_worker.uuid, "uuid4", lambda: asset_id)
    monkeypatch.setattr(mermaid_worker.subprocess, "run", fake_run)

    with pytest.raises(RenderError):
        asyncio.run(render_mermaid("flowchart TD\n  A --> B", output_dir=str(tmp_path)))

    assert not input_path.exists()


# ── sanitize_mermaid_spec: quote labels containing parens ──────────────

def test_sanitize_quotes_rect_label_with_empty_parens() -> None:
    """The exact failure mode from the Spring Boot run: a rect node label
    that includes `()` like a function-call name. Mermaid rejects it
    unquoted because `()` is the rounded-shape delimiter."""
    spec = "flowchart LR\n  A[postProcessBeforeInitialization()] --> B"
    out = sanitize_mermaid_spec(spec)
    assert 'A["postProcessBeforeInitialization()"]' in out


def test_sanitize_quotes_rect_label_with_nested_parens() -> None:
    """Labels with non-empty parens (e.g., `foo(bar)`) should also get quoted."""
    spec = "flowchart LR\n  A[foo(bar)] --> B"
    out = sanitize_mermaid_spec(spec)
    assert 'A["foo(bar)"]' in out


def test_sanitize_quotes_rhombus_label_with_parens() -> None:
    """Same rule applies to decision nodes (`{label}`)."""
    spec = "flowchart LR\n  A --> B{check(x)} --> C"
    out = sanitize_mermaid_spec(spec)
    assert 'B{"check(x)"}' in out


def test_sanitize_quotes_subroutine_label_with_parens() -> None:
    """And to subroutine nodes (`[[label]]`)."""
    spec = "flowchart LR\n  A --> S[[init()]] --> B"
    out = sanitize_mermaid_spec(spec)
    assert 'S[["init()"]]' in out


def test_sanitize_quotes_hexagon_label_with_parens() -> None:
    """And to hexagon nodes (`{{label}}`)."""
    spec = "flowchart LR\n  A --> H{{compute(input)}} --> B"
    out = sanitize_mermaid_spec(spec)
    assert 'H{{"compute(input)"}}' in out


def test_sanitize_leaves_simple_labels_unchanged() -> None:
    """Plain alphanumeric labels don't need quoting — the sanitizer must
    avoid adding noise to specs that already work."""
    spec = "flowchart LR\n  A[Start] --> B[Step 1] --> C[End]"
    out = sanitize_mermaid_spec(spec)
    assert out == spec


def test_sanitize_does_not_double_quote_already_quoted_labels() -> None:
    """If the LLM already quoted a label (or the sanitizer ran twice),
    don't add another layer of quotes."""
    spec = 'flowchart LR\n  A["postProcessBeforeInitialization()"] --> B'
    out = sanitize_mermaid_spec(spec)
    assert out == spec
    # And running it again is also a no-op (idempotence check).
    assert sanitize_mermaid_spec(out) == spec


def test_sanitize_leaves_sequence_diagram_untouched() -> None:
    """sequenceDiagram doesn't use bracket shapes — applying the sanitizer
    here would mangle valid syntax. Must skip non-flowchart specs entirely."""
    spec = (
        "sequenceDiagram\n"
        "  Alice->>Bob: invoke(arg1, arg2)\n"
        "  Note over Alice: setUp()"
    )
    out = sanitize_mermaid_spec(spec)
    assert out == spec


def test_sanitize_class_diagram_does_not_corrupt_class_body_braces() -> None:
    """classDiagram uses `{}` for class bodies — the sanitizer must not
    corrupt those braces when applying note-syntax fixes."""
    spec = (
        "classDiagram\n"
        "  class BeanFactory {\n"
        "    +getBean() Object\n"
        "    +containsBean() bool\n"
        "  }"
    )
    out = sanitize_mermaid_spec(spec)
    assert out == spec


def test_sanitize_strips_whitespace_inside_quoted_edge_label() -> None:
    """The user-reported failure: `| "Broker 1" |` (spaces around the
    quoted label inside the pipes) made Mermaid choke with the same
    parse error as an unquoted label with parens. The sanitizer must
    re-emit tight: `|"Broker 1"|`."""
    spec = 'flowchart LR\n  A["Producer"] -->| "Broker 1" | B["Consumer"]'
    out = sanitize_mermaid_spec(spec)
    assert '|"Broker 1"|' in out
    assert '| "Broker 1" |' not in out


def test_sanitize_strips_whitespace_around_unquoted_edge_label() -> None:
    """Same rule applies to unquoted edge labels: re-emit tight."""
    spec = "flowchart LR\n  A --> | label | B"
    out = sanitize_mermaid_spec(spec)
    assert "|label|" in out


def test_sanitize_removes_empty_quoted_edge_label() -> None:
    """The 1M-RPM article failure: `B -->|""| C` produced a parse error
    ('Expecting TEXT, got PIPE'). The LLM emits an empty quoted label
    when it has no meaningful caption for the edge but feels obligated to
    label every one. We strip `|""|` entirely so the edge renders as a
    plain arrow."""
    spec = 'flowchart LR\n  A["Load Balancer"] --> B["Spring Boot"]\n  B -->|""| C["Cache"]'
    out = sanitize_mermaid_spec(spec)
    assert '|""|' not in out
    assert "B --> C" in out  # collapsed to plain arrow


def test_sanitize_removes_empty_unquoted_edge_label() -> None:
    """`||` (no content between pipes) is another way the LLM produces
    an empty edge — strip those too."""
    spec = "flowchart LR\n  A --> B\n  B --> || C"
    out = sanitize_mermaid_spec(spec)
    assert "||" not in out


def test_sanitize_removes_whitespace_only_quoted_edge_label() -> None:
    """`|"  "|` (quoted, but only whitespace inside) is also effectively
    empty — strip it."""
    spec = 'flowchart LR\n  A --> B\n  B -->|"   "| C'
    out = sanitize_mermaid_spec(spec)
    assert '|"' not in out  # no surviving quoted label
    assert "B --> C" in out


def test_sanitize_state_diagram_strips_quoted_state_ids() -> None:
    """The actual failure case from the user's Kafka run:
    `[*] --> "STOPPED"` blew up with `Expecting 'ID', got 'STRING'`.
    The fix unquotes the state ID."""
    spec = (
        'stateDiagram-v2\n'
        '    [*] --> "STOPPED"\n'
        '    "STOPPED" --> "RUNNING"\n'
        '    "RUNNING" --> [*]'
    )
    out = sanitize_mermaid_spec(spec)
    assert '"STOPPED"' not in out
    assert '"RUNNING"' not in out
    assert "[*] --> STOPPED" in out
    assert "STOPPED --> RUNNING" in out


def test_sanitize_state_diagram_converts_spaces_in_quoted_ids_to_underscores() -> None:
    """`"Long state name"` → `Long_state_name` — produces a valid ID."""
    spec = 'stateDiagram-v2\n    [*] --> "Initial State"\n    "Initial State" --> [*]'
    out = sanitize_mermaid_spec(spec)
    assert "Initial_State" in out
    assert '"Initial State"' not in out


def test_sanitize_state_diagram_preserves_display_label_syntax() -> None:
    """The `state "Long Name" as A` form is LEGAL — quotes there are not
    a mistake. The sanitizer must not touch them."""
    spec = (
        'stateDiagram-v2\n'
        '    state "Pending Approval" as PA\n'
        '    [*] --> PA\n'
        '    PA --> Approved'
    )
    out = sanitize_mermaid_spec(spec)
    # The display-label quotes must survive.
    assert 'state "Pending Approval" as PA' in out


def test_sanitize_state_diagram_preserves_transition_labels_with_parens() -> None:
    """In stateDiagram, the text after `:` is free text — parens, quotes,
    and other punctuation are fine and must not be stripped."""
    spec = (
        "stateDiagram-v2\n"
        "    Running --> Crashed : onError(exception)\n"
        "    Crashed --> Running : retry()"
    )
    out = sanitize_mermaid_spec(spec)
    # Transition labels with parens stay intact.
    assert "onError(exception)" in out
    assert "retry()" in out


def test_sanitize_state_diagram_leaves_bare_ids_alone() -> None:
    """Already-correct stateDiagram must pass through unchanged — the
    sanitizer must not invent quoting noise."""
    spec = (
        "stateDiagram-v2\n"
        "    [*] --> Init\n"
        "    Init --> Running: start()\n"
        "    Running --> [*]"
    )
    out = sanitize_mermaid_spec(spec)
    assert out == spec


def test_sanitize_handles_graph_keyword_in_addition_to_flowchart() -> None:
    """Mermaid accepts both `graph LR` (legacy) and `flowchart LR`. Both
    should trigger the sanitizer."""
    spec = "graph LR\n  A[foo()] --> B"
    out = sanitize_mermaid_spec(spec)
    assert 'A["foo()"]' in out


def test_sanitize_handles_multiple_problem_labels_on_same_line() -> None:
    spec = "flowchart LR\n  A[init()] --> B[run()] --> C[done()]"
    out = sanitize_mermaid_spec(spec)
    assert 'A["init()"]' in out
    assert 'B["run()"]' in out
    assert 'C["done()"]' in out


def test_sanitize_handles_empty_input() -> None:
    """Edge case: empty string in, empty string out — no exceptions."""
    assert sanitize_mermaid_spec("") == ""
    assert sanitize_mermaid_spec("   ") == "   "


# ── Edge labels: |label| between arrow and target ──────────────────────

def test_sanitize_quotes_edge_label_with_parens() -> None:
    """The Spring Boot user-report bug: an edge label like
    `|chain.doFilter()|` breaks the parser identically to an unquoted
    node label. The original hotfix only handled node labels; this
    extends coverage to edge labels."""
    spec = 'flowchart LR\n  A["src"] -->|chain.doFilter()| B["dst"]'
    out = sanitize_mermaid_spec(spec)
    assert '|"chain.doFilter()"|' in out
    # And the surrounding node labels stay intact.
    assert 'A["src"]' in out
    assert 'B["dst"]' in out


def test_sanitize_quotes_edge_label_with_nested_parens() -> None:
    spec = "flowchart LR\n  A -->|invoke(arg)| B"
    out = sanitize_mermaid_spec(spec)
    assert '|"invoke(arg)"|' in out


def test_sanitize_leaves_plain_edge_label_unchanged() -> None:
    """Edge labels without problematic chars should not get extra quoting."""
    spec = "flowchart LR\n  A -->|HTTP socket| B"
    out = sanitize_mermaid_spec(spec)
    assert out == spec


def test_sanitize_does_not_double_quote_edge_label() -> None:
    """If the LLM already quoted the edge label, leave it alone."""
    spec = 'flowchart LR\n  A -->|"chain.doFilter()"| B'
    out = sanitize_mermaid_spec(spec)
    assert out == spec
    # Idempotent — running twice doesn't accumulate quotes.
    assert sanitize_mermaid_spec(out) == spec


def test_sanitize_handles_multiple_edge_labels_on_same_line() -> None:
    spec = "flowchart LR\n  A -->|init()| B -->|run()| C"
    out = sanitize_mermaid_spec(spec)
    assert '|"init()"|' in out
    assert '|"run()"|' in out


def test_sanitize_handles_various_arrow_shapes() -> None:
    """Mermaid supports `-->`, `---`, `==>`, `-.->`, etc. before the
    pipe-delimited edge label. The regex shouldn't care about the arrow
    shape — it only looks for `|...|`."""
    specs = [
        ("flowchart LR\n  A ---|fn()| B", '|"fn()"|'),
        ("flowchart LR\n  A ==>|fn()| B", '|"fn()"|'),
        ("flowchart LR\n  A -.->|fn()| B", '|"fn()"|'),
    ]
    for spec, expected in specs:
        out = sanitize_mermaid_spec(spec)
        assert expected in out, f"expected {expected!r} in {out!r}"


def test_sanitize_handles_full_failing_spec_from_real_run() -> None:
    """End-to-end reproduction of the spec that failed in the user's
    Spring Boot generation: a 'request pipeline' flowchart whose edge
    labels mixed plain text with parens. Every parens-containing edge
    label must be quoted in the output; plain ones left untouched."""
    spec = '''flowchart LR
    A["Client"] -->|HTTP socket| B["Embedded Tomcat"]
    B -->|chain.doFilter()| C["FilterChain"]
    C -->|invokes method| D["DispatcherServlet"]
    D -->|maps URL to handler| E["HandlerMapping"]'''

    out = sanitize_mermaid_spec(spec)

    # Plain edge labels left alone.
    assert "|HTTP socket|" in out
    assert "|invokes method|" in out
    assert "|maps URL to handler|" in out
    # The one with parens gets quoted.
    assert '|"chain.doFilter()"|' in out
    # Node labels (already quoted by the LLM) remain unchanged.
    assert 'A["Client"]' in out
    assert 'E["HandlerMapping"]' in out


# ── classDiagram: note syntax conversion ───────────────────────────────

def test_sanitize_class_diagram_converts_note_right_of_syntax() -> None:
    """The Java 8-25 article failure: `note right of Shape: text` is
    sequence-diagram syntax and causes `Expecting 'STR', got 'ALPHA'` in
    classDiagram. The sanitizer must convert it to `note for Shape "text"`."""
    spec = (
        "classDiagram\n"
        "  Shape <|-- Circle\n"
        "  Shape <|-- Triangle\n"
        "  note right of Shape: Compiler enforces exhaustive pattern matching."
    )
    out = sanitize_mermaid_spec(spec)
    assert 'note for Shape "Compiler enforces exhaustive pattern matching."' in out
    assert "note right of" not in out


def test_sanitize_class_diagram_converts_note_left_of_syntax() -> None:
    """`note left of` is equally invalid in classDiagram — same fix."""
    spec = (
        "classDiagram\n"
        "  class Foo\n"
        "  note left of Foo: Some description."
    )
    out = sanitize_mermaid_spec(spec)
    assert 'note for Foo "Some description."' in out
    assert "note left of" not in out


def test_sanitize_class_diagram_strips_literal_backslash_n_from_note_text() -> None:
    r"""The LLM embeds `\n` as a literal two-character sequence in note text.
    Mermaid classDiagram notes don't support embedded newlines — collapse them
    to spaces so the note renders as a single line."""
    spec = (
        "classDiagram\n"
        "  Shape <|-- Circle\n"
        r"  note right of Shape: Compiler enforces exhaustive\npattern matching."
    )
    out = sanitize_mermaid_spec(spec)
    assert "\\n" not in out
    assert 'note for Shape "Compiler enforces exhaustive pattern matching."' in out


def test_sanitize_class_diagram_leaves_valid_note_syntax_unchanged() -> None:
    """A classDiagram that already uses `note for X "text"` must pass through
    unchanged — the sanitizer must not corrupt already-correct specs."""
    spec = (
        "classDiagram\n"
        "  class Shape\n"
        '  note for Shape "Already correct note syntax."'
    )
    out = sanitize_mermaid_spec(spec)
    assert out == spec


def test_sanitize_class_diagram_leaves_class_body_braces_unchanged() -> None:
    """The existing guard: classDiagram `{}` class body braces must not be
    corrupted by the new sanitizer (regression for the original guard test)."""
    spec = (
        "classDiagram\n"
        "  class BeanFactory {\n"
        "    +getBean() Object\n"
        "    +containsBean() bool\n"
        "  }"
    )
    out = sanitize_mermaid_spec(spec)
    assert out == spec


def test_generate_spec_applies_sanitizer_to_llm_output() -> None:
    """Integration: when the LLM returns a spec with unquoted parens,
    generate_spec must return the sanitized version — the caller of
    generate_spec then passes the spec to mermaid-cli, so this is the
    last chance to fix it.

    The mock client uses Anthropic's messages.create() shape (a list of
    content blocks) to match the interface that generate_spec now expects.
    """
    from types import SimpleNamespace

    raw_llm_output = "flowchart LR\n  A[postProcessBeforeInitialization()] --> B[End]"

    class _Messages:
        async def create(self, **kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=raw_llm_output)],
                stop_reason="end_turn",
            )

    class _Client:
        messages = _Messages()

    from pipeline.schemas.models import VisualIntent
    from render.mermaid_worker import generate_spec

    intent = VisualIntent(description="x", rationale="y")
    out = asyncio.run(generate_spec(intent, _Client()))

    assert 'A["postProcessBeforeInitialization()"]' in out
