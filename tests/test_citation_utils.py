import uuid

from pipeline.schemas.models import EvidenceSpan
from pipeline.workers.citation_utils import (
    resolve_citations,
    scrub_em_dashes,
)


def test_scrub_em_dashes_replaces_em_and_en_dashes() -> None:
    text = "Some idea — with an em-dash. And another – with an en-dash. Range 1–3."
    out = scrub_em_dashes(text)
    assert "—" not in out
    assert "–" not in out
    assert "Some idea, with an em-dash." in out
    assert "And another - with an en-dash." in out
    assert "Range 1-3." in out


def test_scrub_em_dashes_handles_no_surrounding_spaces() -> None:
    out = scrub_em_dashes("word—word and other—case.")
    assert "word, word" in out
    assert "other, case" in out


def test_resolve_citations_numbers_in_first_appearance_order() -> None:
    span_a = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://a.example",
        source_title="A Title",
        content="...",
    )
    span_b = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://b.example",
        source_title="B Title",
        content="...",
    )
    markdown = (
        f"First claim [src:{span_b.span_id}]. "
        f"Second claim [src:{span_a.span_id}]. "
        f"Third claim re-cites B [src:{span_b.span_id}]."
    )

    out = resolve_citations(markdown, [span_a, span_b])

    # B appears first so it becomes [1]; A becomes [2].
    assert "First claim [1]." in out
    assert "Second claim [2]." in out
    assert "Third claim re-cites B [1]." in out
    assert "## Sources" in out
    assert f"1. [B Title](https://b.example)" in out
    assert f"2. [A Title](https://a.example)" in out


def test_resolve_citations_falls_back_to_url_when_no_title() -> None:
    span = EvidenceSpan(
        source_url="https://only-url.example",
        source_title="",
        content="...",
    )
    markdown = f"A claim [src:{span.span_id}]."
    out = resolve_citations(markdown, [span])
    assert "1. [https://only-url.example](https://only-url.example)" in out


def test_resolve_citations_strips_unresolved_markers_and_cleans_whitespace() -> None:
    span = EvidenceSpan(
        source_url="https://known.example",
        source_title="Known",
        content="...",
    )
    unknown_id = uuid.uuid4()
    markdown = (
        f"Known claim [src:{span.span_id}]. "
        f"Unknown claim [src:{unknown_id}] keeps flowing."
    )

    out = resolve_citations(markdown, [span])

    assert f"[src:{unknown_id}]" not in out
    assert "Known claim [1]." in out
    assert "Unknown claim keeps flowing." in out
    # No double spaces left over.
    assert "  " not in out


def test_resolve_citations_no_markers_omits_sources_section() -> None:
    out = resolve_citations("Plain text with no citations.", [])
    assert out == "Plain text with no citations."
    assert "## Sources" not in out


# ── Sprint 4: canonical-URL deduplication ─────────────────────────────

def test_resolve_citations_dedupes_two_spans_pointing_to_same_url() -> None:
    """The Spring Boot bug: search + gap-fill return the same Stack Overflow
    page as two different EvidenceSpans (different chunks). Both get cited
    in the article. The old behavior numbered them [1] and [2]; the new
    behavior numbers BOTH as [1] and lists the URL only once."""
    url = "https://stackoverflow.com/questions/12345/example"
    span_a = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url=url,
        source_title="Stack Overflow question",
        content="First chunk.",
    )
    span_b = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url=url,  # Same URL, different span_id.
        source_title="Stack Overflow question",
        content="Second chunk.",
    )
    markdown = (
        f"First claim [src:{span_a.span_id}]. "
        f"Second claim [src:{span_b.span_id}]."
    )

    out = resolve_citations(markdown, [span_a, span_b])

    # Both citations collapse to [1].
    assert "First claim [1]." in out
    assert "Second claim [1]." in out
    # And the URL appears ONCE in the Sources section, not twice.
    assert out.count(url) == 1


def test_resolve_citations_treats_tracking_params_as_same_url() -> None:
    """Two spans pointing to the same article — one with utm_ tracking
    params, one without — must dedupe to a single citation."""
    span_clean = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://docs.spring.io/spring-boot/reference/",
        source_title="Spring Boot Reference",
        content="...",
    )
    span_tracked = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://docs.spring.io/spring-boot/reference/?utm_source=twitter&utm_medium=social",
        source_title="Spring Boot Reference",
        content="...",
    )
    markdown = (
        f"A [src:{span_clean.span_id}]. B [src:{span_tracked.span_id}]."
    )

    out = resolve_citations(markdown, [span_clean, span_tracked])

    assert "A [1]." in out
    assert "B [1]." in out
    # Source section lists ONE entry.
    assert out.count("Spring Boot Reference") == 1


def test_resolve_citations_treats_trailing_slash_as_same_url() -> None:
    """URL canonicalization strips trailing slashes — `/foo` and `/foo/`
    must collapse to one citation."""
    span_a = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://example.com/article",
        source_title="Example",
        content="...",
    )
    span_b = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://example.com/article/",
        source_title="Example",
        content="...",
    )
    markdown = (
        f"A [src:{span_a.span_id}]. B [src:{span_b.span_id}]."
    )

    out = resolve_citations(markdown, [span_a, span_b])

    assert "A [1]." in out and "B [1]." in out


def test_resolve_citations_distinct_urls_still_get_distinct_numbers() -> None:
    """The dedup must NOT merge citations to different URLs that happen to
    share a domain. This is the negative test for the new behavior."""
    span_a = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://docs.spring.io/page-a",
        source_title="Page A",
        content="...",
    )
    span_b = EvidenceSpan(
        span_id=uuid.uuid4(),
        source_url="https://docs.spring.io/page-b",  # Different path.
        source_title="Page B",
        content="...",
    )
    markdown = f"A [src:{span_a.span_id}]. B [src:{span_b.span_id}]."

    out = resolve_citations(markdown, [span_a, span_b])

    assert "A [1]." in out
    assert "B [2]." in out
    assert "1. [Page A]" in out
    assert "2. [Page B]" in out
