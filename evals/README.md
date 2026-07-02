# Quality Regression Evals

Unit tests prove the pipeline runs; this harness measures whether prompt or
model changes make the ARTICLES better or worse.

## Workflow

1. **Generate the golden set** — for each entry in `topics.yaml`, generate an
   article on the **Best** preset (UI or CLI) and collect the markdown files
   into one directory, named by topic id (e.g. `golden/kafka-design-patterns.md`).
2. **Grade** — `python -m evals.run_eval golden/`
   Each article is graded 3× by the strong model against `rubric.md`
   (per-axis median reported, defects quoted), plus the free mechanical
   checks: banned stock phrases and 1-space-indented code blocks.
   Results land in `evals/results/<timestamp>.json` + a markdown summary.
3. **Compare before merging any prompt change** —
   `python -m evals.run_eval golden-new/ --baseline evals/results/<previous>.json`
   The summary shows per-axis deltas. A change that drops an axis by ≥ 0.5
   median points across the set is a regression: revert or iterate.

Single article spot-check: `python -m evals.grade path/to/article.md`

## Notes

- Grading uses the same provider setup as generation (`main._anthropic_client`
  + `pipeline/model_config.py`), so the provider pin / key rules apply.
- LLM grading is noisy even with medians. Trust deltas ≥ 0.5; ignore ±0.5
  wiggle on a single article.
- `results/` is git-ignored; commit a results file manually when you want to
  pin a baseline.
