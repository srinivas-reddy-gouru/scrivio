import re

from pipeline.schemas.models import EvidenceSpan
from pipeline.workers.search_worker import canonical_url

CITATION_PATTERN = re.compile(r"\[src:([^\]]+)\]")

# Em-dash and horizontal-bar → comma+space, regardless of surrounding spacing.
# En-dash → hyphen (preserve spacing — ranges like "1–3" become "1-3").
_EM_DASH_RE = re.compile(r"\s*[—―]\s*")


def scrub_em_dashes(markdown: str) -> str:
    """Remove em/en-dashes that slipped past the LLM prompts."""
    markdown = _EM_DASH_RE.sub(", ", markdown)
    markdown = markdown.replace("–", "-")
    markdown = re.sub(r" {2,}", " ", markdown)
    markdown = re.sub(r" ([.,;:!?])", r"\1", markdown)
    return markdown


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
    # Stripped markers can leave double spaces or space-before-punctuation.
    body = re.sub(r" {2,}", " ", body)
    body = re.sub(r" ([.,;:!?])", r"\1", body)
    # Collapse runs of the SAME bracketed number — `[4][4]` becomes `[4]`,
    # `[4][4][4]` becomes `[4]`. This catches the case where the drafter
    # emitted `[src:UUID-A][src:UUID-A]` (same span cited twice in a row)
    # or two different UUIDs that share a canonical URL and therefore
    # resolved to the same citation number.
    body = re.sub(r"(\[\d+\])\1+", r"\1", body)

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
