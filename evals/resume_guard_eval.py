"""Does the resume guard hold? Deterministic: no model, no network, no cost.

    python -m evals.resume_guard_eval                 # the current corpus
    python -m evals.resume_guard_eval --corpus v1 --json report.json

Each case is a resume, and what a model is imagined to have returned for
it, written by hand to be wrong in one specific way (or right, to check
the guard leaves good work alone). The guard runs, and the result is held
against what the case says must be true.

This measures ONE thing: whether facts survive. It says nothing about
whether the prose is good or whether the checklist score went up, and it
does not run a model, so it says nothing about how often a real model
makes these mistakes. It says what happens when one does.

Exit status is 0 when every case behaves as recorded, and 1 otherwise.
"As recorded" includes the known gaps, which are required to go on
failing: a gap that closes has to be promoted to an ordinary case, so
that the list of gaps stays a true list.

SO EXIT STATUS 0 DOES NOT MEAN EVERY CASE PASSED. It means nothing
changed. The known gaps are inventions the guard does not catch, and
the last line of the output counts them. With --strict the exit status
is 1 while any remain, for anyone who wants a run that is green only
when the guard catches everything in the corpus.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from evals.corpus_models import Record, ResumeCase, ResumeCorpus, ResumeCorpusV2
from pipeline.schemas.models import StructuredResume, TailoredResume
from pipeline.workers.resume_fact_guard import validate_model_output

CORPORA = Path(__file__).resolve().parent / "corpus"
CURRENT = "v2"
SHAPES = {"v1": ResumeCorpus}           # every later version is ResumeCorpusV2

Guard = Callable[..., TailoredResume]


class CaseResult(BaseModel):
    id: str
    category: str
    outcome: str                     # held | failed | gap | gap_closed
    problems: list[str]
    known_gap: str = ""


class Report(BaseModel):
    corpus: str
    cases: int
    held: int
    failed: int
    gaps: int
    gaps_closed: int
    by_category: dict[str, dict[str, int]]
    results: list[CaseResult]

    @property
    def as_recorded(self) -> bool:
        return self.failed == 0 and self.gaps_closed == 0


def load(version: str = CURRENT) -> ResumeCorpus | ResumeCorpusV2:
    path = CORPORA / version / "resume_cases.json"
    shape = SHAPES.get(version, ResumeCorpusV2)
    return shape.model_validate_json(path.read_text(encoding="utf-8"))


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _records(found, name: str, title: str) -> list[Record]:
    return sorted(
        (Record(name=getattr(r, name), title=getattr(r, title),
                start=r.startDate, end=r.endDate) for r in found),
        key=lambda r: (r.name, r.start, r.title))


def problems_with(case: ResumeCase, guarded: TailoredResume) -> list[str]:
    text = "\n".join(_strings(guarded.resume.model_dump()))
    warnings = "\n".join(guarded.warnings)
    expect, problems = case.expect, []

    problems += [f"missing: {t!r}" for t in expect.must_contain if t not in text]
    problems += [f"still present: {t!r}" for t in expect.must_not_contain if t in text]
    problems += [f"no warning mentions {t!r}" for t in expect.warnings_must_mention
                 if t not in warnings]
    if expect.placeholders is not None and text.count("[METRIC]") != expect.placeholders:
        problems.append(
            f"{text.count('[METRIC]')} placeholder(s), expected {expect.placeholders}")
    if expect.work is not None:
        found = _records(guarded.resume.work, "name", "position")
        wanted = sorted(expect.work, key=lambda r: (r.name, r.start, r.title))
        if found != wanted:
            problems.append(f"work records are {[r.model_dump() for r in found]}")
    if expect.education is not None:
        found = _records(guarded.resume.education, "institution", "studyType")
        wanted = sorted(expect.education, key=lambda r: (r.name, r.start, r.title))
        if found != wanted:
            problems.append(f"education records are {[r.model_dump() for r in found]}")
    return problems


def evaluate(corpus: ResumeCorpus | ResumeCorpusV2,
             guard: Guard = validate_model_output) -> Report:
    results: list[CaseResult] = []
    for case in corpus.cases:
        earlier = getattr(case, "baseline", None)
        guarded = guard(
            StructuredResume.model_validate(case.original.model_dump()),
            TailoredResume.model_validate(case.model_output.model_dump()),
            baseline=(TailoredResume.model_validate(earlier.model_dump())
                      if earlier is not None else None),
            user_text=case.user_text, jd_text=case.jd_text)
        problems = problems_with(case, guarded)
        if case.known_gap:
            outcome = "gap" if problems else "gap_closed"
        else:
            outcome = "failed" if problems else "held"
        results.append(CaseResult(
            id=case.id, category=case.category, outcome=outcome,
            problems=problems, known_gap=case.known_gap))

    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        by_category.setdefault(r.category, {}).setdefault(r.outcome, 0)
        by_category[r.category][r.outcome] += 1
    count = lambda outcome: sum(r.outcome == outcome for r in results)  # noqa: E731
    return Report(
        corpus=corpus.version, cases=len(results), held=count("held"),
        failed=count("failed"), gaps=count("gap"), gaps_closed=count("gap_closed"),
        by_category=by_category, results=results)


def render(report: Report) -> str:
    lines = [
        f"Resume fact preservation, corpus {report.corpus}",
        f"{report.cases} cases: {report.held} held, {report.failed} failed, "
        f"{report.gaps} known gap(s)"
        + (f", {report.gaps_closed} gap(s) now closed" if report.gaps_closed else ""),
        "",
    ]
    for category, counts in sorted(report.by_category.items()):
        lines.append(f"  {category:22} " + ", ".join(
            f"{n} {outcome}" for outcome, n in sorted(counts.items())))
    for title, outcome in (("Failed", "failed"), ("Known gaps", "gap"),
                           ("Gaps that have closed: promote these", "gap_closed")):
        chosen = [r for r in report.results if r.outcome == outcome]
        if not chosen:
            continue
        lines += ["", f"{title}:"]
        for r in chosen:
            lines.append(f"  {r.id}")
            lines += [f"      {p}" for p in r.problems]
            if r.known_gap:
                lines.append(f"      why: {r.known_gap}")
    caught = report.held
    lines += ["", (
        f"Result: the guard does what is expected of it in {caught} of {report.cases} cases."
        + (f" It does NOT catch the other {report.gaps}, listed above as known gaps."
           if report.gaps else "")
        + (f" {report.failed} FAILED." if report.failed else "")
        + (" Nothing has changed since this was recorded."
           if report.as_recorded else " This differs from what was recorded."))]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", default=CURRENT)
    parser.add_argument("--json", type=Path, help="also write the report here")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 while any known gap remains")
    args = parser.parse_args(argv)

    report = evaluate(load(args.corpus))
    print(render(report))
    if args.json:
        args.json.write_text(
            json.dumps(report.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    if args.strict and report.gaps:
        return 1
    return 0 if report.as_recorded else 1


if __name__ == "__main__":
    sys.exit(main())
