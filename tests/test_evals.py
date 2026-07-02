"""Tests for the eval harness: median aggregation, mechanical checks, and the
grade_article flow with a mocked grader (never a live LLM call)."""
import asyncio
import json
from types import SimpleNamespace

from evals.grade import AXES, grade_article, median_scores, mechanical_report


def _run_payload(score: int) -> dict:
    return {
        "scores": {a: {"score": score, "justification": f"j{score}"} for a in AXES},
        "defects": [{"quote": f"quote-{score}", "issue": "issue"}],
    }


def test_median_scores_takes_per_axis_median() -> None:
    runs = [_run_payload(2), _run_payload(4), _run_payload(5)]
    out = median_scores(runs)
    for axis in AXES:
        assert out[axis]["score"] == 4
        assert out[axis]["justification"] == "j4"  # from the median run


def test_median_scores_survives_missing_axes() -> None:
    broken = {"scores": {"factual_accuracy": {"score": 3, "justification": "ok"}}}
    out = median_scores([broken])
    assert out["factual_accuracy"]["score"] == 3
    assert out["code_correctness"]["score"] is None


def test_mechanical_report_flags_both_check_types() -> None:
    md = (
        "The structural fix is X.\n\n"
        "```java\n"
        "class A {\n"
        " int corrupted;\n"
        "}\n"
        "```"
    )
    report = mechanical_report(md)
    assert "The structural fix is" in report["banned_phrases"]
    assert report["singlespace_indented_code_blocks"] == ["```java"]

    clean = mechanical_report("Fine prose.\n\n```java\nclass B {\n    int ok;\n}\n```")
    assert clean == {"banned_phrases": [], "singlespace_indented_code_blocks": []}


def test_grade_article_medians_runs_and_dedupes_defects(monkeypatch, tmp_path) -> None:
    article = tmp_path / "a.md"
    article.write_text("Some article prose.\n", encoding="utf-8")

    # The grader returns different scores per call: median must win, and the
    # duplicate quote across runs must appear once.
    payloads = [_run_payload(2), _run_payload(5), _run_payload(4)]
    payloads[2]["defects"] = [{"quote": "quote-5", "issue": "dupe"}]  # same as run 2
    calls = {"n": 0}

    class FakeMessages:
        async def create(self, **kwargs):
            payload = payloads[calls["n"]]
            calls["n"] += 1
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(payload))]
            )

    import evals.grade as grade_mod
    monkeypatch.setattr(
        grade_mod, "_grading_client",
        lambda: SimpleNamespace(messages=FakeMessages()),
    )

    result = asyncio.run(grade_article(article))
    assert calls["n"] == 3
    assert result["scores"]["factual_accuracy"]["score"] == 4
    assert result["total"] == 4 * len(AXES)
    quotes = [d["quote"] for d in result["defects"]]
    assert quotes.count("quote-5") == 1          # deduped across runs
    assert result["mechanical"]["banned_phrases"] == []
