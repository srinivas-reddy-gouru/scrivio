import asyncio
from types import SimpleNamespace

from pipeline.workers.topic_classifier import (
    classify_topic_breadth,
    heuristic_classify,
    llm_classify,
)


# ── Heuristic classifier ────────────────────────────────────────────────

def test_heuristic_classifies_umbrella_concepts_as_broad_undefined() -> None:
    """Umbrella concepts (database, security, ML) have multiple sub-domains.
    Always require clarification — never broad_defined."""
    for topic in ["database", "security", "machine learning", "performance", "cloud"]:
        assert heuristic_classify(topic) == "broad_undefined", (
            f"{topic!r} should classify as broad_undefined"
        )


def test_heuristic_classifies_single_product_names_as_broad_defined() -> None:
    """One- or two-word product/framework names with no specificity → broad_defined.
    These have a clear default scope so the system can offer a sensible fallback."""
    for topic in ["Spring Boot", "Django", "React", "PostgreSQL", "Kafka", "Redis"]:
        assert heuristic_classify(topic) == "broad_defined", (
            f"{topic!r} should classify as broad_defined"
        )


def test_heuristic_classifies_questions_as_narrow() -> None:
    """Questions starting with 'why' or 'how' are inherently narrow — the
    user has already pinned down what they want to know."""
    narrow_topics = [
        "Why does Postgres VACUUM block writes?",
        "How to debug a memory leak in Node",
        "Why is my Django ORM doing N+1 queries",
    ]
    for topic in narrow_topics:
        assert heuristic_classify(topic) == "narrow", f"{topic!r} should be narrow"


def test_heuristic_classifies_comparisons_as_narrow() -> None:
    """Comparison verbs (vs, versus, compare) mean the user has picked the
    axis. Generation should proceed without asking."""
    for topic in ["React vs Vue", "Postgres versus MySQL", "compare gRPC and REST"]:
        assert heuristic_classify(topic) == "narrow", f"{topic!r} should be narrow"


def test_heuristic_classifies_topics_with_code_symbols_as_narrow() -> None:
    """Function calls, annotations, namespaces all indicate specificity."""
    for topic in ["@Transactional in Spring", "asyncio.gather() patterns",
                  "std::move performance", "useState() vs useReducer()"]:
        assert heuristic_classify(topic) == "narrow", f"{topic!r} should be narrow"


def test_heuristic_returns_none_for_ambiguous_middle() -> None:
    """3-4 word topics without narrow markers are too uncertain to bucket
    with heuristics alone — return None so the LLM tiebreaks."""
    for topic in ["Spring Boot Actuator", "Kafka stream processing",
                  "TypeScript generic constraints"]:
        assert heuristic_classify(topic) is None, (
            f"{topic!r} should defer to LLM"
        )


def test_heuristic_handles_empty_topic_as_broad_undefined() -> None:
    """An empty topic gets the strongest 'must ask' signal — broad_undefined
    forces the clarification flow rather than silently generating something
    based on whatever defaults the brief would invent."""
    assert heuristic_classify("") == "broad_undefined"
    assert heuristic_classify("   ") == "broad_undefined"


def test_heuristic_classifies_long_specific_topics_as_narrow() -> None:
    """5+ word topics without narrow markers usually still imply specificity
    just by their length — they describe a scenario."""
    topic = "Building a real-time analytics dashboard with Apache Pinot"
    # 8 words, no narrow markers in the marker list.
    assert heuristic_classify(topic) == "narrow"


# ── LLM fallback ────────────────────────────────────────────────────────

class _MockMessages:
    def __init__(self, breadth: str) -> None:
        self.breadth = breadth
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name="submit_topic_classification",
                    input={"breadth": self.breadth, "reasoning": "mock"},
                )
            ]
        )


class _MockClient:
    def __init__(self, breadth: str) -> None:
        self.messages = _MockMessages(breadth)


def test_llm_classify_returns_breadth_from_tool_use() -> None:
    client = _MockClient("broad_defined")
    result = asyncio.run(llm_classify("Spring Boot Actuator", "", client))
    assert result == "broad_defined"
    # Sanity check: the prompt actually contained the topic.
    call = client.messages.calls[0]
    assert "Spring Boot Actuator" in call["messages"][0]["content"]
    assert call["tool_choice"]["name"] == "submit_topic_classification"


def test_classify_topic_breadth_skips_llm_when_heuristic_confident() -> None:
    """The heuristic should short-circuit the LLM call for clear cases."""
    client = _MockClient("narrow")  # Would return narrow if called.

    # "database" is in _BROAD_UNDEFINED_TOPICS → heuristic decides.
    result = asyncio.run(classify_topic_breadth("database", "", client))
    assert result == "broad_undefined"
    # LLM must not have been called.
    assert client.messages.calls == []


def test_classify_topic_breadth_invokes_llm_for_ambiguous_topic() -> None:
    """For 3-4 word topics without narrow markers, the LLM tiebreaks."""
    client = _MockClient("broad_defined")
    result = asyncio.run(
        classify_topic_breadth("Spring Boot Actuator", "", client)
    )
    assert result == "broad_defined"
    assert len(client.messages.calls) == 1
