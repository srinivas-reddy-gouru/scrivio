"""Grade every article in a directory and write a comparable results file.

Usage:
    python -m evals.run_eval <articles-dir> [--baseline evals/results/<old>.json]

Writes evals/results/<timestamp>.json (machine-comparable) and a markdown
summary next to it. With --baseline, per-axis deltas against the previous
results are printed and included in the summary — that is the whole point:
never merge a prompt change without knowing which way the golden set moved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from evals.grade import AXES, grade_article

RESULTS_DIR = Path(__file__).parent / "results"


async def _run(articles_dir: Path) -> list[dict]:
    results = []
    for md_path in sorted(articles_dir.glob("**/*.md")):
        print(f"grading {md_path} …")
        results.append(await grade_article(md_path))
    return results


def _load_baseline(path: str | None) -> dict[str, dict]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {Path(r["article"]).name: r for r in data.get("results", [])}


def _fmt_delta(new, old) -> str:
    if new is None or old is None:
        return ""
    d = round(new - old, 1)
    return f" ({'+' if d >= 0 else ''}{d})" if d else " (=)"


def _summary_markdown(results: list[dict], baseline: dict[str, dict]) -> str:
    header = "| article | " + " | ".join(a[:12] for a in AXES) + " | total |"
    sep = "|---" * (len(AXES) + 2) + "|"
    rows = [header, sep]
    for r in results:
        name = Path(r["article"]).name
        base = baseline.get(name, {})
        cells = []
        for axis in AXES:
            score = r["scores"][axis]["score"]
            old = base.get("scores", {}).get(axis, {}).get("score") if base else None
            cells.append(f"{score}{_fmt_delta(score, old)}")
        total_old = base.get("total") if base else None
        rows.append(
            f"| {name} | " + " | ".join(cells)
            + f" | {r['total']}{_fmt_delta(r['total'], total_old)} |"
        )
        mech = r["mechanical"]
        if mech["banned_phrases"] or mech["singlespace_indented_code_blocks"]:
            rows.append(
                f"| ⚠ {name} mechanical | "
                + " | ".join([""] * len(AXES))
                + f" | {', '.join(mech['banned_phrases'])}"
                  f"{' + bad indent' if mech['singlespace_indented_code_blocks'] else ''} |"
            )
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("articles_dir", type=Path)
    parser.add_argument("--baseline", default=None,
                        help="previous results JSON to diff against")
    args = parser.parse_args()

    results = asyncio.run(_run(args.articles_dir))
    baseline = _load_baseline(args.baseline)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_json = RESULTS_DIR / f"{stamp}.json"
    out_json.write_text(json.dumps(
        {"generated_at": stamp, "baseline": args.baseline, "results": results},
        indent=2,
    ), encoding="utf-8")

    summary = _summary_markdown(results, baseline)
    out_md = RESULTS_DIR / f"{stamp}.md"
    out_md.write_text(summary + "\n", encoding="utf-8")

    print(f"\n{summary}\n\nresults: {out_json}\nsummary: {out_md}")


if __name__ == "__main__":
    main()
