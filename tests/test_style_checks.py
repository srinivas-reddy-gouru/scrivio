"""Tests for the mechanical template-signature and indentation checks."""
from pipeline.workers.style_checks import (
    BANNED_PHRASES,
    find_banned_phrases,
    find_singlespace_indented_code_blocks,
)


def test_banned_phrases_load_from_shared_fragment() -> None:
    # The list is parsed from the same fragment the prompts include, so the
    # checker and the models can never drift apart.
    assert "The structural fix is" in BANNED_PHRASES
    assert "worth naming" in BANNED_PHRASES
    assert "delve" in BANNED_PHRASES


def test_banned_phrase_in_prose_triggers() -> None:
    md = "The structural fix is a transactional outbox. One operational detail matters."
    hits = find_banned_phrases(md)
    assert "The structural fix is" in hits
    assert "One operational detail" in hits


def test_banned_phrase_is_case_insensitive() -> None:
    assert find_banned_phrases("the STRUCTURAL FIX IS simple.") == ["The structural fix is"]


def test_banned_phrase_inside_code_block_does_not_trigger() -> None:
    md = (
        "Clean prose here.\n\n"
        "```java\n"
        "// The structural fix is applied in code comments\n"
        "String robust = delve();\n"
        "```\n\n"
        "More clean prose."
    )
    assert find_banned_phrases(md) == []


def test_banned_phrase_inside_inline_code_does_not_trigger() -> None:
    assert find_banned_phrases("Call `robust_delve()` to start.") == []


def test_singlespace_indent_flags_corrupted_block() -> None:
    corrupted = "```java\npublic class A {\n private int x;\n}\n```"
    clean = "```java\npublic class B {\n    private int y;\n}\n```"
    md = f"prose\n\n{corrupted}\n\nprose\n\n{clean}"
    offenders = find_singlespace_indented_code_blocks(md)
    assert offenders == ["```java"] or len(offenders) == 1


def test_singlespace_indent_passes_clean_blocks() -> None:
    md = "```python\ndef f():\n    return 1\n```\n\n```\nno indent at all\n```"
    assert find_singlespace_indented_code_blocks(md) == []
