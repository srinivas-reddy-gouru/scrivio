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
