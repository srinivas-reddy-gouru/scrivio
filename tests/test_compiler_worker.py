import asyncio
from types import SimpleNamespace

from pipeline.schemas.models import (
    ArticlePlan,
    ArticleRequest,
    DraftPackage,
    DraftSection,
    PublishedArticle,
    RenderAsset,
    VisualIntent,
)
from pipeline.workers.compiler_worker import compile_all_levels, compile_level


class MockMessages:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response_text = self.responses.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=response_text)]
        )


class MockAnthropicClient:
    def __init__(self, responses: list[str]) -> None:
        self.messages = MockMessages(responses)


def make_draft() -> DraftPackage:
    request = ArticleRequest(
        topic="Database indexes",
        explanation_level="intermediate",
        audience_role="software engineer",
    )
    plan = ArticlePlan(
        request=request,
        sections=[],
        claims=[],
        visual_intents=[],
        evidence_span_ids=["abc123", "def456"],
    )
    sections = [
        DraftSection(
            title="Indexes",
            content=(
                "Indexes speed up selective reads [src:abc123]. "
                "They add write overhead [src:def456]."
            ),
            citation_ids=["abc123", "def456"],
        )
    ]
    return DraftPackage(
        plan=plan,
        sections=sections,
        raw_markdown=sections[0].content,
    )


def test_compile_level_basic_returns_published_article_with_citations() -> None:
    draft = make_draft()
    client = MockAnthropicClient(
        [
            (
                "# Database indexes\n\n"
                "An index is like a lookup card [src:abc123]. "
                "It can slow writes because it must be updated [src:def456]."
            )
        ]
    )

    article = asyncio.run(compile_level(draft, "basic", client))

    assert isinstance(article, PublishedArticle)
    assert article.request == draft.plan.request
    assert article.title == "Database indexes"
    assert "[src:abc123]" in article.markdown
    assert "[src:def456]" in article.markdown
    assert "WARNING" not in article.markdown
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 16000
    assert "Level: basic" in call["system"]
    assert "Audience: software engineer" in call["system"]


def test_compile_level_appends_warning_when_citation_missing() -> None:
    draft = make_draft()
    client = MockAnthropicClient(
        ["# Database indexes\n\nIndexes speed up reads [src:abc123]."]
    )

    article = asyncio.run(compile_level(draft, "advanced", client))

    assert "[src:abc123]" in article.markdown
    assert "WARNING: missing citations from original draft: def456" in article.markdown


def test_compile_all_levels_returns_all_three_keys() -> None:
    draft = make_draft()
    client = MockAnthropicClient(
        [
            "Basic rewrite [src:abc123] [src:def456].",
            "Intermediate rewrite [src:abc123] [src:def456].",
            "Advanced rewrite [src:abc123] [src:def456].",
        ]
    )

    articles = asyncio.run(compile_all_levels(draft, client))

    assert set(articles) == {"basic", "intermediate", "advanced"}
    assert all(isinstance(article, PublishedArticle) for article in articles.values())
    assert len(client.messages.calls) == 3


def test_compile_level_substitutes_diagram_placeholder_with_mermaid_block() -> None:
    """When the LLM output contains <!-- DIAGRAM:{id} --> and a matching asset
    exists, the compiler replaces the placeholder with a mermaid fenced block."""
    draft = make_draft()
    intent = VisualIntent(
        description="Sequence diagram",
        format="mermaid",
        rationale="Visualizes the lookup flow.",
        section_title="Indexes",
    )
    asset = RenderAsset(
        intent=intent,
        spec="flowchart LR\n  A --> B",
        output_path="/tmp/x.svg",
        qa_passed=True,
    )

    compiled_with_placeholder = (
        f"# Database indexes\n\n"
        f"An index is like a lookup card [src:abc123].\n\n"
        f"<!-- DIAGRAM:{intent.intent_id} -->\n\n"
        f"It can slow writes [src:def456]."
    )
    client = MockAnthropicClient([compiled_with_placeholder])

    article = asyncio.run(compile_level(draft, "basic", client, assets=[asset]))

    assert f"<!-- DIAGRAM:{intent.intent_id} -->" not in article.markdown
    assert "```mermaid" in article.markdown
    assert "flowchart LR" in article.markdown
    assert "A --> B" in article.markdown


def test_compile_level_drops_placeholder_silently_when_no_matching_asset() -> None:
    """A placeholder with no matching asset (render failed, asset not generated)
    must be removed — leaving HTML comments in published markdown looks broken."""
    draft = make_draft()
    compiled_with_orphan = (
        "# Database indexes\n\n"
        "Indexes speed up reads [src:abc123].\n\n"
        "<!-- DIAGRAM:nonexistent-intent-id -->\n\n"
        "Writes pay a cost [src:def456]."
    )
    client = MockAnthropicClient([compiled_with_orphan])

    article = asyncio.run(compile_level(draft, "basic", client, assets=[]))

    assert "<!-- DIAGRAM:" not in article.markdown
    assert "```mermaid" not in article.markdown
    # Surrounding content preserved.
    assert "Indexes speed up reads [src:abc123]." in article.markdown
    assert "Writes pay a cost [src:def456]." in article.markdown


def test_compile_level_works_without_assets_argument() -> None:
    """Compiler must remain backward-compatible: callers that omit assets get
    the same behavior as before (placeholders, if any, are dropped silently)."""
    draft = make_draft()
    client = MockAnthropicClient(
        ["# Database indexes\n\nReads [src:abc123]. Writes [src:def456]."]
    )

    article = asyncio.run(compile_level(draft, "intermediate", client))

    assert isinstance(article, PublishedArticle)
    assert "[src:abc123]" in article.markdown
