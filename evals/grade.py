"""Grade one generated article against evals/rubric.md.

Uses the existing provider setup (main._anthropic_client + model_config), so
whatever key/provider/pin works for generation works for grading — no
hand-rolled API client. LLM grading is noisy, so each article is graded
GRADER_RUNS times and the per-axis MEDIAN is reported.

Usage:
    python -m evals.grade path/to/article.md
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
from pathlib import Path

from pipeline.model_config import get_model
from pipeline.workers.style_checks import (
    find_banned_phrases,
    find_singlespace_indented_code_blocks,
)

AXES = (
    "factual_accuracy",
    "code_correctness",
    "diagram_clarity",
    "citation_quality",
    "prose_naturalness",
    "overall_publishable",
)

# Three runs is the smallest count with a meaningful median; five barely
# tightens the estimate and costs 66% more.
GRADER_RUNS = 3

# Quality judgment needs the strong tier; the cap keeps a grading run cheap
# (the output is six scores, six justifications, and a short defect list).
_GRADER_PRESET = "best"
_GRADER_MAX_TOKENS = 1600

_RUBRIC = (Path(__file__).parent / "rubric.md").read_text(encoding="utf-8")

_SYSTEM_PROMPT = (
    "You are a strict technical-content grader. Grade the article against the "
    "rubric. Judge as a reader, not as the author. Respond with STRICT JSON "
    "only (no prose, no markdown fences):\n"
    '{"scores": {"<axis>": {"score": 1-5, "justification": "one line"}, ...},\n'
    ' "defects": [{"quote": "exact text from the article", "issue": "what is wrong"}]}\n'
    f"The axes are exactly: {', '.join(AXES)}.\n\nRUBRIC:\n{_RUBRIC}"
)


def _grading_client():
    from main import _anthropic_client
    from pipeline.schemas.models import ArticleRequest

    return _anthropic_client(ArticleRequest(topic="eval-grading"))


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text


async def _grade_once(markdown: str, client) -> dict:
    response = await client.messages.create(
        model=get_model("critic", _GRADER_PRESET),
        max_tokens=_GRADER_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"ARTICLE:\n\n{markdown}"}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "") or ""
    return json.loads(_strip_fences(raw))


def median_scores(runs: list[dict]) -> dict:
    """Per-axis median score across grading runs; justification is taken
    from the run whose score equals the median (first match)."""
    result: dict = {}
    for axis in AXES:
        entries = [
            r["scores"][axis] for r in runs
            if isinstance(r.get("scores", {}).get(axis, None), dict)
            and isinstance(r["scores"][axis].get("score"), (int, float))
        ]
        if not entries:
            result[axis] = {"score": None, "justification": "grader returned no score"}
            continue
        med = statistics.median(e["score"] for e in entries)
        just = next(
            (e["justification"] for e in entries if e["score"] == med),
            entries[0].get("justification", ""),
        )
        result[axis] = {"score": med, "justification": just}
    return result


def mechanical_report(markdown: str) -> dict:
    """The deterministic checks — free, and immune to grader mood."""
    return {
        "banned_phrases": find_banned_phrases(markdown),
        "singlespace_indented_code_blocks":
            find_singlespace_indented_code_blocks(markdown),
    }


async def grade_article(path: str | Path) -> dict:
    markdown = Path(path).read_text(encoding="utf-8")
    client = _grading_client()

    runs: list[dict] = []
    for _ in range(GRADER_RUNS):
        try:
            runs.append(await _grade_once(markdown, client))
        except Exception as exc:  # noqa: BLE001 — a failed run shrinks the median pool
            print(f"  grader run failed: {exc}", file=sys.stderr)
    if not runs:
        raise RuntimeError(f"All {GRADER_RUNS} grading runs failed for {path}")

    scores = median_scores(runs)
    defects: list[dict] = []
    seen_quotes: set[str] = set()
    for run in runs:
        for defect in run.get("defects") or []:
            quote = str(defect.get("quote", ""))[:200]
            if quote and quote not in seen_quotes:
                seen_quotes.add(quote)
                defects.append({"quote": quote, "issue": str(defect.get("issue", ""))})

    numeric = [v["score"] for v in scores.values() if v["score"] is not None]
    return {
        "article": str(path),
        "scores": scores,
        "total": round(sum(numeric), 1) if numeric else None,
        "defects": defects,
        "mechanical": mechanical_report(markdown),
        "runs": len(runs),
    }


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m evals.grade <article.md>", file=sys.stderr)
        raise SystemExit(2)
    result = asyncio.run(grade_article(sys.argv[1]))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
