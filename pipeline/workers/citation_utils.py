import re

from pipeline.schemas.models import EvidenceSpan
from pipeline.workers.search_worker import canonical_url

CITATION_PATTERN = re.compile(r"\[src:([^\]]+)\]")

# Em-dash and horizontal-bar → " - " (spaced hyphen), regardless of
# surrounding spacing. A hyphen stays grammatical where the old ", "
# replacement manufactured comma splices; the prompts now instruct the
# models to restructure dash constructions themselves, so this regex is a
# last-resort safety net, not the primary mechanism.
# En-dash → hyphen (preserve spacing — ranges like "1–3" become "1-3").
_EM_DASH_RE = re.compile(r"\s*[—―]\s*")

# Fenced code blocks (``` … ```, language tags included, ```mermaid too).
# The capture group makes re.split() return them at ODD indices, alternating
# with prose segments at even indices.
_FENCED_BLOCK_RE = re.compile(r"(```.*?```)", re.DOTALL)

# Inline code spans within prose. Same capture-group/odd-index trick.
_INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")


def _scrub_plain_text(text: str) -> str:
    """The actual scrub, safe only for prose with no code in it."""
    text = _EM_DASH_RE.sub(" - ", text)
    text = text.replace("–", "-")
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r" ([.,;:!?])", r"\1", text)
    return text


def _scrub_prose_segment(segment: str) -> str:
    """Scrub one between-fences segment, leaving inline code spans intact.

    If the segment contains a stray ``` (an UNCLOSED fence — the paired ones
    were already captured by _FENCED_BLOCK_RE), everything from that opening
    fence onward is left untouched: mangling maybe-code is worse than
    leaving maybe-prose unscrubbed.
    """
    fence_idx = segment.find("```")
    tail = ""
    if fence_idx != -1:
        segment, tail = segment[:fence_idx], segment[fence_idx:]

    parts = _INLINE_CODE_RE.split(segment)
    scrubbed = "".join(
        part if i % 2 else _scrub_plain_text(part) for i, part in enumerate(parts)
    )
    return scrubbed + tail


def _apply_to_prose(markdown: str, fn) -> str:
    """Apply *fn* to prose only; fenced blocks pass through byte-for-byte.

    EVERY text-wide cleanup in this module must go through this helper.
    History, twice over: a whole-document whitespace collapse destroyed
    code indentation once in scrub_em_dashes (fixed there, documented
    below) and then AGAIN in resolve_citations' marker cleanup — caught
    by the matchup evals as uniformly 1-space-indented code in every
    pipeline article, running last and undoing the first fix's work.
    """
    parts = _FENCED_BLOCK_RE.split(markdown)
    return "".join(part if i % 2 else fn(part) for i, part in enumerate(parts))


def scrub_em_dashes(markdown: str) -> str:
    """Remove em/en-dashes that slipped past the LLM prompts — prose only.

    Fenced code blocks (including ```mermaid) and inline code spans pass
    through byte-for-byte. The scrub used to run on the whole document and
    its whitespace-collapsing step destroyed code indentation (see the
    1-space-indented Java/SQL snippets in examples/kafka-design-patterns.md);
    dashes inside code are code, not typography.
    """
    return _apply_to_prose(markdown, _scrub_prose_segment)


def resolve_citations(markdown: str, spans: list[EvidenceSpan]) -> str:
    """Replace [src:UUID] markers with [N] refs and append a Sources section.

    Numbers are assigned per UNIQUE CANONICAL URL, not per span_id. Two
    spans that point to the same article (which happens when search and
    gap-fill both return the same URL with different chunks) get the SAME
    citation number — and the source appears only ONCE in the Sources list.

    Before this change: 16 citations, 8 unique URLs (lots of `[4]=[5]`,
    `[7]=[8]` duplicates). After: 8 citations, 8 unique URLs.

    Numbering follows first-appearance order in the markdown.
    Markers referencing unknown spans are stripped.
    Articles with no resolvable citations are returned without a Sources
    section.
    """
    span_by_id = {str(span.span_id): span for span in spans}
    found = CITATION_PATTERN.findall(markdown)

    # Walk citation markers in document order; assign each NEW canonical
    # URL the next available number. Spans pointing to an already-seen
    # canonical URL inherit that URL's number.
    span_id_to_number: dict[str, int] = {}
    url_to_number: dict[str, int] = {}
    number_to_first_span_id: dict[int, str] = {}
    next_number = 1

    for span_id in found:
        if span_id in span_id_to_number:
            continue  # Already numbered.
        span = span_by_id.get(span_id)
        if span is None:
            continue  # Unknown span_id — will be stripped by replace().
        url_key = canonical_url(span.source_url)
        if url_key in url_to_number:
            span_id_to_number[span_id] = url_to_number[url_key]
        else:
            span_id_to_number[span_id] = next_number
            url_to_number[url_key] = next_number
            number_to_first_span_id[next_number] = span_id
            next_number += 1

    def replace(match: re.Match) -> str:
        sid = match.group(1)
        number = span_id_to_number.get(sid)
        return f"[{number}]" if number is not None else ""

    body = CITATION_PATTERN.sub(replace, markdown)

    # Stripped markers can leave double spaces or space-before-punctuation —
    # in PROSE. Code indentation is exactly a run of spaces; this cleanup
    # must never see it (it ran document-wide once and de-indented every
    # code block in every article — the matchup evals' top defect).
    def _tidy_marker_gaps(segment: str) -> str:
        segment = re.sub(r" {2,}", " ", segment)
        segment = re.sub(r" ([.,;:!?])", r"\1", segment)
        # Collapse runs of the SAME bracketed number — `[4][4]` becomes
        # `[4]` (same span cited twice in a row, or two UUIDs sharing a
        # canonical URL and thus a citation number).
        return re.sub(r"(\[\d+\])\1+", r"\1", segment)

    body = _apply_to_prose(body, _tidy_marker_gaps)

    if not number_to_first_span_id:
        return body.rstrip()

    lines = ["## Sources", ""]
    for number in sorted(number_to_first_span_id):
        # Use the FIRST span we saw for this URL as the label source —
        # gives the most stable title attribution across reruns.
        span = span_by_id[number_to_first_span_id[number]]
        label = span.source_title.strip() or span.source_url
        lines.append(f"{number}. [{label}]({span.source_url})")

    return f"{body.rstrip()}\n\n" + "\n".join(lines)
