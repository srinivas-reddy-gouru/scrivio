"""Tests for truncation detection and recovery in the humanizer."""
import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import ArticlePlan, ArticleRequest, ArticleSection, Claim
from pipeline.workers.humanization_worker import (
    _trim_to_last_clean_paragraph,
    _generate_closing_section,
)


# ── _trim_to_last_clean_paragraph ────────────────────────────────────────────

def test_trim_finds_last_paragraph_break() -> None:
    """Normal case: text has a double-newline in the final 40%; trim there."""
    text = "Paragraph one.\n\nParagraph two.\n\nParagraph three half-fi"
    result = _trim_to_last_clean_paragraph(text)
    assert result == "Paragraph one.\n\nParagraph two."


def test_trim_falls_back_to_sentence_boundary() -> None:
    """No paragraph break in the final 40% → fall back to last sentence end."""
    # One long paragraph with no double-newline, but ends mid-sentence.
    text = "A" * 200 + ". More content here that goes on and on without stopping mid"
    result = _trim_to_last_clean_paragraph(text)
    # Should end at the period after the A block.
    assert result.endswith(".")
    assert "mid" not in result


def test_trim_closes_unclosed_code_fence_before_trimming() -> None:
    """Truncation inside a code block: close the fence first, then trim."""
    text = (
        "Some prose before the code.\n\n"
        "```java\n"
        "public class Foo {\n"
        "    // truncated here"
    )
    result = _trim_to_last_clean_paragraph(text)
    # The unclosed ``` should be removed; result should end at the prose paragraph.
    assert "```java" not in result
    assert result.strip() == "Some prose before the code."


def test_trim_handles_even_fence_count_correctly() -> None:
    """Even number of ``` (closed code block) should NOT be truncated."""
    text = (
        "Before code.\n\n"
        "```java\nint x = 1;\n```\n\n"
        "After code. This sentence is cut mid"
    )
    result = _trim_to_last_clean_paragraph(text)
    # Should trim at the last paragraph break before "After code."
    assert "```java" in result      # the complete code block stays
    assert "mid" not in result


def test_trim_returns_text_unchanged_when_no_clean_boundary_found() -> None:
    """If the text is short and has no clean boundary in the final 40-80%,
    return as-is rather than discarding too much content."""
    text = "Short text with no paragraph break or sentence end"
    result = _trim_to_last_clean_paragraph(text)
    assert result == text


# ── _generate_closing_section ────────────────────────────────────────────────

def _make_plan(section_titles: list[str]) -> ArticlePlan:
    from pipeline.schemas.models import StoryBrief
    brief = StoryBrief(
        thesis="Spring AI bridges the gap between Java and LLMs.",
        angle="explainer",
        reader_pain_point="Java devs can't use AI tools easily.",
        key_insight="Spring AI applies familiar patterns to AI.",
        hook_seed="You open a browser and find only Python tutorials.",
        suggested_title="Spring AI for Java Engineers",
    )
    sections = [
        ArticleSection(title=t, claim_ids=[])
        for t in section_titles
    ]
    return ArticlePlan(
        request=ArticleRequest(topic="Spring AI"),
        brief=brief,
        sections=sections,
        claims=[],
        visual_intents=[],
        evidence_span_ids=[],
    )


class _MockMessages:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(text=self.response_text)],
            stop_reason="end_turn",
        )


class _MockClient:
    def __init__(self, response_text: str = "Closing paragraph here.") -> None:
        self.messages = _MockMessages(response_text)


def test_generate_closing_includes_uncovered_sections() -> None:
    """The user message sent to the model must name sections that were NOT
    reached so the closing can direct the reader to them."""
    plan = _make_plan(["Intro", "Core Architecture", "RAG Pipelines", "Tool Calling"])
    # Only Intro and Core Architecture appear in the polished text.
    polished_so_far = "... Intro content ...\n\nCore Architecture content ..."
    client = _MockClient()

    asyncio.run(_generate_closing_section(plan, polished_so_far, client))

    user_content = client.messages.calls[0]["messages"][0]["content"]
    assert "RAG Pipelines" in user_content
    assert "Tool Calling" in user_content


def test_generate_closing_uses_haiku_model() -> None:
    """Closing generation must use the cheap Haiku model, not Sonnet —
    it's a small targeted call and Haiku is sufficient."""
    plan = _make_plan(["A", "B"])
    client = _MockClient()

    asyncio.run(_generate_closing_section(plan, "A content", client))

    assert client.messages.calls[0]["model"] == "claude-haiku-4-5-20251001"


def test_generate_closing_returns_model_text() -> None:
    """The returned string must be the model's response text, stripped."""
    plan = _make_plan(["A"])
    client = _MockClient("  This is the closing.  ")

    result = asyncio.run(_generate_closing_section(plan, "A content", client))

    assert result == "This is the closing."


def test_generate_closing_max_tokens_is_small() -> None:
    """The closing call must not request a large token budget — it's 150-250
    words of prose, so 400 tokens is the right ceiling."""
    plan = _make_plan(["A"])
    client = _MockClient()

    asyncio.run(_generate_closing_section(plan, "A content", client))

    assert client.messages.calls[0]["max_tokens"] == 400


# ── Code-fence protection through the polish pass ────────────────────────────
# Eval evidence (matchup-20260807): the polish/refinement rewrites mangled
# code-block indentation in every pipeline article. Code now bypasses the
# LLM entirely: fences → markers → restored byte-identical.

from pipeline.workers.humanization_worker import (  # noqa: E402
    _protect_code_fences,
    _restore_code_fences,
    humanize_markdown,
)

_CODE_MD = """## Section

Intro prose.

```python
def f():
    if True:
        return 1
```

Middle prose.

```mermaid
flowchart LR
  A --> B
```

Closing prose.
"""


def test_protect_and_restore_roundtrip() -> None:
    protected, blocks = _protect_code_fences(_CODE_MD)
    assert len(blocks) == 2
    assert "```" not in protected
    assert "<!-- CODE:0 -->" in protected and "<!-- CODE:1 -->" in protected
    assert _restore_code_fences(protected, blocks) == _CODE_MD


def test_restore_logs_and_survives_dropped_marker() -> None:
    protected, blocks = _protect_code_fences(_CODE_MD)
    mangled = protected.replace("<!-- CODE:1 -->", "")  # model ate a marker
    restored = _restore_code_fences(mangled, blocks)
    assert "def f():" in restored          # surviving marker restored
    assert "flowchart LR" not in restored  # dropped block is lost, not corrupted


def test_polish_pass_cannot_touch_code() -> None:
    """Even a hostile model that rewrites everything it sees cannot change
    code: it never receives it. The mock returns its input with all prose
    replaced — code must come back byte-identical."""
    class _RewritingMessages(_MockMessages):
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            content = kwargs["messages"][-1]["content"]
            body = content.split("article_markdown:\n", 1)[1]
            # Hostile pass: de-indent every line (the observed failure mode),
            # which would wreck any code it could reach.
            wrecked = "\n".join(line.lstrip() for line in body.splitlines())
            return SimpleNamespace(
                content=[SimpleNamespace(text=wrecked)], stop_reason="end_turn",
            )

    client = _MockClient()
    client.messages = _RewritingMessages("")
    plan = _make_plan(["Section"])
    result = asyncio.run(humanize_markdown(_CODE_MD, plan, client))
    assert "    if True:\n        return 1" in result   # indentation intact
    assert "flowchart LR" in result
    # And the model genuinely never saw the code.
    sent = client.messages.calls[0]["messages"][-1]["content"]
    assert "def f():" not in sent
    assert "protected_code: 2 fenced" in sent
