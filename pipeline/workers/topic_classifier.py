"""Classifies an incoming article request as narrow vs. broad.

The classification is the gate that decides whether `/generate` asks the user
for clarification (broad + unsteered) or proceeds straight to generation
(narrow, or broad-but-already-steered).

Two-stage design:
1. Python heuristics catch the clear cases without spending an LLM call. Most
   real inputs fall into a clear bucket — "PostgreSQL" is obviously broad,
   "Why does my Django ORM emit N+1 queries on prefetch_related" is obviously
   narrow. The heuristic decides those in microseconds.
2. The LLM only runs when the heuristic is ambiguous (medium-length topic
   with no obvious specificity markers). The LLM call uses tool_use with a
   tiny schema, so it's cheap.
"""
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


TopicBreadth = Literal["narrow", "broad_defined", "broad_undefined"]


# Words that, when present, almost always indicate a narrow / specific request.
# These are the verbs and symptoms a real engineer types when debugging or
# building something specific.
_NARROW_MARKERS = frozenset({
    "why", "how", "debug", "fix", "compare", "vs", "versus", "between",
    "error", "exception", "fails", "failing", "broken", "slow", "fast",
    "blocking", "deadlock", "timeout", "leak", "crash", "race",
    "migrate", "migrating", "upgrade", "downgrade",
    "best", "worst", "should", "shouldn't", "when",
    "tutorial", "guide", "build", "building", "implement", "implementing",
})

# Specific symbol markers — punctuation that almost guarantees specificity.
_NARROW_SYMBOLS = ("@", "::", "()", ".", "_", "->", "=>", ":")


# Umbrella concepts that ALWAYS need clarification, even if they look "defined".
# These are huge surface areas where the user almost certainly wants only one
# sub-domain. Listed here so a single-word match goes straight to undefined.
_BROAD_UNDEFINED_TOPICS = frozenset({
    "database", "databases", "db",
    "security", "cybersecurity",
    "performance",
    "machine learning", "ml", "ai", "artificial intelligence",
    "cloud", "cloud computing",
    "devops",
    "networking", "network",
    "frontend", "backend", "fullstack", "full-stack",
    "testing",
    "architecture",
    "microservices",
    "api", "apis",
    "data", "data engineering", "data science",
    "observability", "monitoring",
})


def heuristic_classify(topic: str, extra_context: str = "") -> TopicBreadth | None:
    """Return a breadth label if the heuristic is confident; otherwise None.

    None means "the LLM should weigh in" — used when the topic is in the
    fuzzy middle (e.g., "Spring Boot Actuator", "Tailwind CSS plugins").
    """
    normalized = topic.strip().lower()
    word_count = len(normalized.split())

    if not normalized:
        # Empty topic — treat as broad_undefined so the user is forced to
        # provide steering rather than getting a generic article.
        return "broad_undefined"

    # Umbrella concept matches go straight to undefined regardless of length.
    if normalized in _BROAD_UNDEFINED_TOPICS:
        return "broad_undefined"

    # Specific punctuation (function calls, namespaces, dotted paths, type
    # annotations) strongly implies a narrow request.
    if any(symbol in topic for symbol in _NARROW_SYMBOLS):
        return "narrow"

    # Specific verbs / symptom words → narrow.
    tokens = set(re.findall(r"[a-z]+", normalized))
    if tokens & _NARROW_MARKERS:
        return "narrow"

    # Long topics (5+ words) without narrow markers are unusual but generally
    # imply enough specificity to count as narrow.
    if word_count >= 5:
        return "narrow"

    # 1-2 word topics with no narrowing signal — these are the canonical
    # "broad" inputs (Spring Boot, PostgreSQL, Kubernetes, React).
    if word_count <= 2:
        return "broad_defined"

    # 3-4 word topics with no narrowing signal land in the ambiguous middle.
    # Examples: "Spring Boot testing", "Kafka stream processing". Defer to LLM.
    return None


# ── LLM tiebreak path ────────────────────────────────────────────────

class _BreadthClassification(BaseModel):
    breadth: TopicBreadth = Field(
        description=(
            "narrow = topic names a specific problem, comparison, or symptom. "
            "broad_defined = a specific product/framework with a clear default "
            "scope (e.g., Spring Boot Actuator). "
            "broad_undefined = an umbrella concept with multiple sub-domains "
            "the user has not chosen between (e.g., database performance)."
        )
    )
    reasoning: str = Field(default="", description="One sentence; for logs.")


_CLASSIFIER_PROMPT = (
    "You classify how broad a technical article topic is. The output gates "
    "whether the article generator asks the user clarifying questions or "
    "proceeds directly to generation.\n\n"
    "narrow: the user has already pinned down what they want. Specific verbs "
    "('why', 'how to', 'fix', 'compare X vs Y'), specific errors, named "
    "functions/classes/files, or 5+ word topics with clear focus.\n\n"
    "broad_defined: a single product, framework, or language name with a "
    "clear default scope. Reader of the article will expect foundations and "
    "common usage patterns. Examples: 'Spring Boot', 'Django', 'React', "
    "'PostgreSQL', 'Redis'.\n\n"
    "broad_undefined: an umbrella concept with multiple legitimate "
    "sub-domains the user has not chosen. Generating an article without "
    "clarifying which sub-domain would be a guess. Examples: 'database', "
    "'security', 'machine learning', 'performance', 'cloud'.\n\n"
    "If extra_context is provided, weigh it: extra_context that names "
    "specific aspects converts a broad topic into a steered (narrow-like) "
    "request, BUT for breadth classification you should still classify the "
    "TOPIC itself — the caller will decide separately whether to ask "
    "clarification given the extra_context."
)


_CLASSIFIER_TOOL: dict = {
    "name": "submit_topic_classification",
    "description": "Submit the breadth classification for this topic.",
    "input_schema": _BreadthClassification.model_json_schema(),
}


async def llm_classify(topic: str, extra_context: str, client) -> TopicBreadth:
    user_content = (
        f"topic: {topic}\n"
        f"extra_context: {extra_context or '(none)'}"
    )
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=_CLASSIFIER_PROMPT,
        tools=[_CLASSIFIER_TOOL],
        tool_choice={"type": "tool", "name": "submit_topic_classification"},
        messages=[{"role": "user", "content": user_content}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return _BreadthClassification.model_validate(tool_use.input).breadth


async def classify_topic_breadth(
    topic: str,
    extra_context: str,
    client,
) -> TopicBreadth:
    """Heuristic-first classification with LLM tiebreak on ambiguity."""
    heuristic = heuristic_classify(topic, extra_context)
    if heuristic is not None:
        return heuristic
    return await llm_classify(topic, extra_context, client)
