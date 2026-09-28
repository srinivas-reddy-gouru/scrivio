# Evaluations

Unit tests show that the code runs. These ask whether what it produces is any
good, which is a different question and a harder one.

| Evaluation | Asks | Calls a model | Run in CI |
| --- | --- | --- | --- |
| `resume_guard_eval` | When a model writes something untrue into a resume, is it caught? | No | Yes |
| `interview_grading_eval` | Does the interview grader score answers correctly and consistently? | Yes, on request only | No |
| `matchup` | Is the article pipeline better than one prompt to the same model? | Yes | No |
| `run_eval`, `grade` | Did a prompt change make articles better or worse? | Yes | No |

**What has been run.** The resume guard evaluation, which is deterministic.
The article matchups recorded under `results/`. **Nothing else.** No live
evaluation of resume tailoring quality or of interview grading has been run,
and no person has scored anything. Where this file describes a procedure, it
is a procedure and not a result.

## Resume: are facts preserved?

```bash
python -m evals.resume_guard_eval
```

`corpus/v1/resume_cases.json` holds 38 cases. Each is a resume and what a
model is imagined to have returned for it, written by hand to be wrong in one
specific way, or right, to check that good work is left alone:

| Category | Cases | For example |
| --- | --- | --- |
| Unsupported fact | 11 | An invented figure. A figure moved from one employer to another. A raised title. |
| Missing information | 3 | The original has no end date, and the model supplies one. |
| Promotion | 4 | Two roles at one employer, with the senior title written onto the earlier role. |
| Reordered entries | 3 | Jobs returned in another order with a figure swapped under cover of it. |
| Format variation | 9 | `2M` written as `2 million`. 287 rounded to 300. A percentage worked out from two real figures. |
| Language variation | 6 | Spanish, German, and Japanese resumes, including `12,5 %` and `2.000.000`. |
| Legitimate edit | 2 | An honest rewording. A figure the candidate typed themselves. |

It measures one thing: whether facts survive. It does not look at the
checklist score or at how the prose reads, and those must not be folded into
it. A rewrite can raise the score and read well and still have invented a
number, which is the case this exists to catch.

It does not run a model, so it does not say how often a real model makes
these mistakes. It says what happens when one does.

**Known gaps.** Three cases are recorded as things the guard does not catch:
the top of a range stated as the figure, an approximation restated as a
minimum, and a stronger verb. All three are changes of wording around a
figure that is itself unchanged. They are in the corpus so that the list is
written down and cannot quietly grow. A known gap is required to keep
failing: if one starts passing, the run fails until it is promoted to an
ordinary case.

**That the cases mean something** is checked by running them with the guard
removed (`tests/test_eval_corpus.py`). Every adversarial case then fails. A
case that passed with no guard at all would be testing nothing.

## Interviews: is the grading right?

```bash
python -m evals.interview_grading_eval                        # prints the plan, calls nothing
python -m evals.interview_grading_eval --live --max-calls 36  # 36 calls to your provider
```

**This has not been run.** It needs a provider, and it spends money or
subscription allowance, so it runs only when asked, and `--max-calls` has to
be at least the number of calls the plan prints. It refuses demo mode and
refuses any canned client, because grading fixed answers measures nothing and
would be saved looking like a result.

`corpus/v1/interview_cases.json` holds 12 answers to 2 questions, written to
separate substance from manner:

| The answer is | What it should show |
| --- | --- |
| Complete, at ordinary length | The top of the scale is reachable |
| Complete, in a third of the words | Length earns nothing |
| Complete, but hesitant | Manner costs nothing |
| Long, fluent, and nearly empty | Fluency is not coverage |
| Confident and wrong | Confidence earns nothing |
| The right vocabulary, nothing said | Buzzwords earn nothing |
| Thin, followed by an instruction to the grader to award 10 | The instruction changes nothing |
| Partial, wrong, empty | The rest of the scale |

A run records, for every call, the provider, the model, the latency, and the
token usage **when the provider reports it**. A subscription command-line
assistant reports none, and then none is recorded. Usage is never estimated
and written down as if it had been measured.

From a run it works out: how often the score lands in the expected band, the
spread of scores for one answer across repeats, the difference within each
pair of answers that differ only in length or manner, and whether the
instruction to the grader moved the score.

### Who judged what

The expected bands in corpus v1 were assigned by the AI assistant that wrote
the cases. **They are not human judgments**, and the corpus file says so in
its own header. Agreement with them is agreement between two models, which is
weaker evidence than it looks: both may share the same blind spots.

To compare against a person:

1. Give them `corpus/v1/interview_cases.json` with `expected_score_min`,
   `expected_score_max`, and `points_covered` removed, and no model scores.
2. They fill in `corpus/v1/human_scores_template.csv`: a score from 0 to 10
   for each answer against its rubric, and their name. One person per file.
3. `python -m evals.interview_grading_eval --score results/<run>.json --human scores.csv`

Their agreement is reported on its own line, under their name. It is never
merged into the band agreement.

### What this can and cannot show

Twelve answers to two questions is a smoke test. It can show that something
is badly wrong: that a long empty answer outscores a short complete one, or
that the grader does what an answer tells it to. It cannot show that grading
is right in general. Do not report a percentage from it as an accuracy
figure. A claim about grading quality needs more questions, several topics
and levels, and more than one person scoring.

## Versions

A corpus is a folder: `corpus/v1`. Once results have been recorded against
it, it does not change. New cases, or corrections to old ones, go into
`corpus/v2`, so that a result always names cases that still exist as they
were when it was run. Until a result has been recorded against v1, v1 may
still be corrected.

## Comparing against a simple baseline

Not built for resumes or interviews. The question to ask is the one the
article matchup asks: does the workflow beat one prompt to the same model
("here is my resume and a posting, tailor it")? The way to ask it:

1. Both arms get the same resumes from `corpus/` and the same postings.
2. Both outputs go through `resume_guard_eval`'s checks **as a measurement,
   not as a repair**: count what each arm invented before anything is fixed.
3. Prose quality is judged blind and position-swapped, as in `matchup.py`,
   and reported apart from the factual count.
4. Losses are published with wins.

Until that is run, this project makes no claim that its resume workflow
beats a single prompt.

# Articles: quality regression

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
