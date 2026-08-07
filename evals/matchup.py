"""Head-to-head eval: the full article pipeline vs "Claude in one prompt".

This answers the question the absolute-grading harness (grade.py) cannot:
is the multi-stage pipeline actually BETTER than a single strong-model
call on the same topic, same provider, same subscription — and by how
much, at what cost?

Design choices that keep the verdict honest:
- The baseline arm is the user's real counterfactual: ONE call to the
  same strong model the pipeline drafts with, no search, no tools.
- Pairwise judging is BLIND (articles labeled A/B, provenance never
  shown) and POSITION-SWAPPED: each judge sees both orders, and an arm
  only "wins" an axis if it wins in both orders — order-flipped verdicts
  count as ties, which cancels position bias.
- Two independent judges when both keys exist (Anthropic strong tier +
  OpenAI gpt-4o), so a single model's self-preference can't decide the
  result alone.
- Cost is measured, not asserted: wall-clock seconds and actual LLM call
  counts per arm (client factories are wrapped with counting proxies).

Usage:
    python -m evals.matchup                # all topics in MATCHUP_TOPICS
    python -m evals.matchup redis-streams  # one topic by id

Writes evals/results/matchup-<stamp>/: the four articles, raw verdicts
JSON, and report.md.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:  # the user's provider selection lives in .env — same setup the app uses
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ModuleNotFoundError:
    pass

import main as app_main
from pipeline.model_config import get_model
from pipeline.schemas.models import ArticleRequest

RESULTS_DIR = Path(__file__).parent / "results"
_RUBRIC = (Path(__file__).parent / "rubric.md").read_text(encoding="utf-8")

# Fresh topics (never generated before → no stage cache, so wall time and
# call counts reflect a real run). Mix: one where live search should help
# (recent releases), one comparison, one evergreen practical guide — so the
# pipeline's web-research advantage is tested but can't decide everything.
MATCHUP_TOPICS = [
    {
        "id": "python-free-threading",
        "topic": "Understanding Python's GIL: what it actually locks, and what "
                 "changes with free-threaded CPython",
        "level": "intermediate",
    },
    {
        "id": "redis-streams-vs-kafka",
        "topic": "Redis Streams vs Kafka: choosing an event backbone for a "
                 "mid-size backend",
        "level": "intermediate",
    },
    {
        "id": "idempotent-rest-apis",
        "topic": "Designing idempotent REST APIs: idempotency keys, safe "
                 "retries, and what exactly-once really means",
        "level": "intermediate",
    },
]

AXES = (
    "factual_accuracy",
    "code_correctness",
    "citation_quality",
    "prose_naturalness",
    "reader_engagement",
    "overall_publishable",
)

_JUDGE_SYSTEM = f"""You are judging two technical articles on the SAME topic, labeled A and B. You do not know how either was produced — judge only what is on the page, as a reader who found it via search.

For each axis, pick "A", "B", or "tie" with a one-line reason. "tie" is a legitimate verdict — do not force differences that are not there. An article that cites sources it clearly cannot verify (invented-looking URLs, vague "studies show") should LOSE citation_quality to an article with fewer but real-looking citations — and to one that honestly cites nothing.

reader_engagement means: would a busy engineer keep reading past the second section? Penalize flat encyclopedic recitation AND forced razzle-dazzle equally.

Respond with STRICT JSON only (no fences):
{{"axes": {{"<axis>": {{"winner": "A|B|tie", "reason": "one line"}}}},
 "overall_winner": "A|B|tie",
 "overall_reason": "two sentences max"}}
Axes exactly: {", ".join(AXES)}.

RUBRIC (for axis definitions where they overlap):
{_RUBRIC}
"""

_BASELINE_PROMPT = """Write a complete technical article on the following topic.

topic: {topic}
audience: software engineer
level: {level}

Requirements: a real article, not an outline — engaging opening, coherent narrative, code examples where they genuinely help, honest about trade-offs. If you cite sources, cite only ones you are confident actually exist, with URLs; do not invent citations. Aim for 1500–2200 words. Output pure markdown starting with the # title."""


# ── LLM-call counting ────────────────────────────────────────────────

class _CountingProxy:
    """Wraps a provider client; counts every terminal *create/parse call
    through any attribute chain (messages.create, chat.completions.create,
    beta.chat.completions.parse)."""

    def __init__(self, target, counter: dict):
        self._target, self._counter = target, counter

    def __getattr__(self, name):
        attr = getattr(self._target, name)
        if name in ("create", "parse") and callable(attr):
            counter = self._counter

            async def _counted(*args, **kwargs):
                counter["calls"] += 1
                return await attr(*args, **kwargs)

            return _counted
        if callable(attr):
            return attr  # any other method: uncounted pass-through
        if isinstance(attr, (str, int, float, bool, dict, list, tuple)) or attr is None:
            return attr
        return _CountingProxy(attr, self._counter)  # namespace (messages, chat, …)


# ── Arms ─────────────────────────────────────────────────────────────

async def run_pipeline_arm(topic: dict) -> dict:
    """Full pipeline via generate_article, with counted clients."""
    counter = {"calls": 0}
    real_anthropic, real_openai = app_main._anthropic_client, app_main._openai_client
    app_main._anthropic_client = lambda req: _CountingProxy(real_anthropic(req), counter)
    app_main._openai_client = lambda req: _CountingProxy(real_openai(req), counter)
    request = ArticleRequest(
        topic=topic["topic"], explanation_level=topic["level"],
        skip_clarification=True,
    )
    started = time.monotonic()
    try:
        articles = await app_main.generate_article(request)
    finally:
        app_main._anthropic_client, app_main._openai_client = real_anthropic, real_openai
    article = articles[topic["level"]]
    return {
        "markdown": article.markdown,
        "seconds": round(time.monotonic() - started, 1),
        "llm_calls": counter["calls"],
    }


async def run_baseline_arm(topic: dict) -> dict:
    """One prompt, same strong model the pipeline drafts with."""
    request = ArticleRequest(topic=topic["topic"], explanation_level=topic["level"])
    client = app_main._anthropic_client(request)
    started = time.monotonic()
    response = await client.messages.create(
        model=get_model("drafting", "balanced"),
        max_tokens=8192,
        messages=[{"role": "user", "content": _BASELINE_PROMPT.format(
            topic=topic["topic"], level=topic["level"],
        )}],
    )
    markdown = next((b.text for b in response.content if b.type == "text"), "")
    return {
        "markdown": markdown.strip(),
        "seconds": round(time.monotonic() - started, 1),
        "llm_calls": 1,
    }


# ── Judges ───────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text


async def _judge_anthropic(topic: str, a_md: str, b_md: str) -> dict:
    request = ArticleRequest(topic="matchup-judging")
    client = app_main._anthropic_client(request)
    response = await client.messages.create(
        model=get_model("critic", "best"),
        max_tokens=1200,
        system=_JUDGE_SYSTEM,
        messages=[{"role": "user", "content":
                   f"topic: {topic}\n\nARTICLE A:\n\n{a_md}\n\n{'=' * 40}\n\nARTICLE B:\n\n{b_md}"}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(_strip_fences(raw))


async def _judge_openai(topic: str, a_md: str, b_md: str) -> dict:
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("no OPENAI_API_KEY")
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    response = await client.chat.completions.create(
        model="gpt-4o",
        max_tokens=1200,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content":
             f"topic: {topic}\n\nARTICLE A:\n\n{a_md}\n\n{'=' * 40}\n\nARTICLE B:\n\n{b_md}"},
        ],
    )
    return json.loads(_strip_fences(response.choices[0].message.content or ""))


_JUDGES = {"claude": _judge_anthropic, "gpt-4o": _judge_openai}


def _swap_verdict(verdict: dict) -> dict:
    """Map a verdict issued on swapped inputs back to canonical labels
    (canonical A = pipeline). A judge that says 'A' on swapped inputs is
    picking the baseline."""
    flip = {"A": "B", "B": "A", "tie": "tie"}
    return {
        "axes": {
            axis: {**entry, "winner": flip.get(entry.get("winner", "tie"), "tie")}
            for axis, entry in (verdict.get("axes") or {}).items()
        },
        "overall_winner": flip.get(verdict.get("overall_winner", "tie"), "tie"),
        "overall_reason": verdict.get("overall_reason", ""),
    }


def _consensus(forward: dict, backward: dict) -> dict:
    """An arm wins an axis only if it wins in BOTH orders; disagreement is
    a tie (kills position bias). Same rule for overall."""
    out: dict = {"axes": {}, "orders": [forward, backward]}
    for axis in AXES:
        f = (forward.get("axes") or {}).get(axis, {}).get("winner", "tie")
        b = (backward.get("axes") or {}).get(axis, {}).get("winner", "tie")
        out["axes"][axis] = f if f == b else "tie"
    f_all = forward.get("overall_winner", "tie")
    b_all = backward.get("overall_winner", "tie")
    out["overall"] = f_all if f_all == b_all else "tie"
    return out


async def judge_pair(topic: str, pipeline_md: str, baseline_md: str) -> dict:
    """All available judges × both orders. Canonical: A=pipeline, B=baseline."""
    verdicts: dict = {}
    for name, judge in _JUDGES.items():
        try:
            forward = await judge(topic, pipeline_md, baseline_md)
            backward_raw = await judge(topic, baseline_md, pipeline_md)
            verdicts[name] = _consensus(forward, _swap_verdict(backward_raw))
        except Exception as exc:  # noqa: BLE001 — a judge failing must not kill the run
            print(f"  judge {name} failed: {exc}", file=sys.stderr)
    return verdicts


# ── Orchestration + report ───────────────────────────────────────────

def _win_counts(verdicts: dict) -> dict:
    counts = {"A": 0, "B": 0, "tie": 0}
    for verdict in verdicts.values():
        for winner in verdict["axes"].values():
            counts[winner] += 1
    return counts


async def run_matchup(topics: list[dict]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = RESULTS_DIR / f"matchup-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []

    for topic in topics:
        print(f"\n=== {topic['id']} ===")
        print("  pipeline arm (full flow, live search)…")
        pipeline = await run_pipeline_arm(topic)
        print(f"    {pipeline['seconds']}s, {pipeline['llm_calls']} LLM calls")
        print("  baseline arm (one prompt)…")
        baseline = await run_baseline_arm(topic)
        print(f"    {baseline['seconds']}s, {baseline['llm_calls']} LLM call")

        (out_dir / f"{topic['id']}-pipeline.md").write_text(
            pipeline["markdown"], encoding="utf-8")
        (out_dir / f"{topic['id']}-baseline.md").write_text(
            baseline["markdown"], encoding="utf-8")

        print("  judging (blind, both orders, all judges)…")
        verdicts = await judge_pair(
            topic["topic"], pipeline["markdown"], baseline["markdown"])
        rows.append({
            "topic": topic, "verdicts": verdicts,
            "pipeline": {k: v for k, v in pipeline.items() if k != "markdown"},
            "baseline": {k: v for k, v in baseline.items() if k != "markdown"},
            "pipeline_words": len(pipeline["markdown"].split()),
            "baseline_words": len(baseline["markdown"].split()),
        })

    (out_dir / "raw.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(_report(rows), encoding="utf-8")
    print(f"\nreport: {out_dir / 'report.md'}")
    return out_dir


def _report(rows: list[dict]) -> str:
    lines = [
        "# Pipeline vs one-prompt baseline",
        "",
        "A = full pipeline (search → verify → draft → edit → polish → critic). "
        "B = one prompt to the same strong model. Blind, position-swapped, "
        "an axis is only won when the judge picks the same arm in both orders.",
        "",
    ]
    overall = {"A": 0, "B": 0, "tie": 0}
    for row in rows:
        t = row["topic"]
        lines.append(f"## {t['id']}")
        lines.append("")
        lines.append(
            f"Cost: pipeline {row['pipeline']['seconds']}s / "
            f"{row['pipeline']['llm_calls']} calls / {row['pipeline_words']}w — "
            f"baseline {row['baseline']['seconds']}s / 1 call / "
            f"{row['baseline_words']}w")
        lines.append("")
        lines.append("| axis | " + " | ".join(row["verdicts"].keys()) + " |")
        lines.append("|---" * (len(row["verdicts"]) + 1) + "|")
        for axis in AXES:
            cells = [row["verdicts"][j]["axes"].get(axis, "tie")
                     for j in row["verdicts"]]
            lines.append(f"| {axis} | " + " | ".join(cells) + " |")
        overall_cells = []
        for judge_name, verdict in row["verdicts"].items():
            overall[verdict["overall"]] += 1
            overall_cells.append(verdict["overall"])
        lines.append("| **overall** | " + " | ".join(
            f"**{c}**" for c in overall_cells) + " |")
        lines.append("")
        for judge_name, verdict in row["verdicts"].items():
            for order_label, order in zip(("A-first", "B-first"), verdict["orders"]):
                reason = order.get("overall_reason", "")
                if reason:
                    lines.append(f"- {judge_name} ({order_label}): {reason}")
        lines.append("")
    lines.append("## Aggregate overall verdicts")
    lines.append("")
    lines.append(f"pipeline wins: {overall['A']} · baseline wins: {overall['B']} "
                 f"· ties/split: {overall['tie']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    wanted = set(sys.argv[1:])
    topics = [t for t in MATCHUP_TOPICS if not wanted or t["id"] in wanted]
    if not topics:
        print(f"unknown topic id(s); known: {[t['id'] for t in MATCHUP_TOPICS]}",
              file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(run_matchup(topics))


if __name__ == "__main__":
    main()
