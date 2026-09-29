"""Does the interview grader grade correctly? A live run, and only on request.

    python -m evals.interview_grading_eval                    # the plan. Calls nothing.
    python -m evals.interview_grading_eval --live --max-calls 36
    python -m evals.interview_grading_eval --score results.json --human scores.csv

Grading is done by a model, so the only way to measure it is to run the
model, and that costs money or subscription allowance. Nothing here runs
unless `--live` is given together with `--max-calls`, which has to be at
least the number of calls the plan prints. That is the point of it: the
number has been read before it is spent.

What a run records for every call: provider, model, latency, and token
usage when the provider reports it. Usage that was not reported is
recorded as not reported. It is never estimated and written down as if
measured.

What is worked out from a run:

  band agreement   how often the score falls in the band the case expects
  consistency      the spread of scores for one answer across repeats
  style pairs      answers covering the same points in a different length
                   or manner should score within two points of each other
  instruction      an answer that tells the grader what to score should
                   score the same as that answer without the instruction

The expected bands in corpus v1 were assigned by the AI assistant that
wrote the cases, not by a person. Agreement with them is agreement
between two models. `--human` takes scores a person gave without seeing
the model's, and reports agreement with those separately and by name.

Twelve answers to two questions is a smoke test. It can show that
something is badly wrong. It cannot show that grading is right in
general, and no result from it should be reported as if it did.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from evals.corpus_models import InterviewCase, InterviewCorpus

CORPORA = Path(__file__).resolve().parent / "corpus"
RESULTS = Path(__file__).resolve().parent / "results"
CURRENT = "v1"
PAIR_TOLERANCE = 2


class Observation(BaseModel):
    case_id: str
    repeat: int
    score: int = Field(ge=0, le=10)
    verdict: str = ""
    latency_seconds: float
    input_tokens: int | None = None      # None: the provider did not report it
    output_tokens: int | None = None


class Run(BaseModel):
    corpus: str
    started: str
    provider: str
    model: str
    preset: str
    repeats: int
    calls: int
    usage: str = Field(description="'measured' when every call reported tokens, "
                                   "'partly measured', or 'not reported'")
    observations: list[Observation]


class CaseScore(BaseModel):
    case_id: str
    kind: str
    scores: list[int]
    median: float
    spread: int
    expected: tuple[int, int]
    in_band: bool


class PairScore(BaseModel):
    case_id: str
    paired_with: str
    kind: str
    difference: float
    within_tolerance: bool


class HumanAgreement(BaseModel):
    judged_by: str
    answers_compared: int
    mean_absolute_difference: float
    within_one_point: float


class Findings(BaseModel):
    cases: list[CaseScore]
    band_agreement: float
    bands_were_assigned_by: str
    largest_spread: int
    pairs: list[PairScore]
    human: HumanAgreement | None = None
    caution: str = (
        "Twelve answers to two questions. This can show that something is badly "
        "wrong. It cannot show that grading is right in general.")


def load(version: str = CURRENT) -> InterviewCorpus:
    path = CORPORA / version / "interview_cases.json"
    return InterviewCorpus.model_validate_json(path.read_text(encoding="utf-8"))


def planned_calls(corpus: InterviewCorpus, repeats: int) -> int:
    return len(corpus.cases) * repeats


# ── Working out what a run shows. No model is involved from here down. ──

def findings(corpus: InterviewCorpus, run: Run,
             human: dict[str, int] | None = None, judged_by: str = "") -> Findings:
    by_case: dict[str, list[int]] = {}
    for seen in run.observations:
        by_case.setdefault(seen.case_id, []).append(seen.score)

    cases: list[CaseScore] = []
    for case in corpus.cases:
        scores = by_case.get(case.id, [])
        if not scores:
            continue
        median = statistics.median(scores)
        cases.append(CaseScore(
            case_id=case.id, kind=case.kind, scores=scores, median=median,
            spread=max(scores) - min(scores),
            expected=(case.expected_score_min, case.expected_score_max),
            in_band=case.expected_score_min <= median <= case.expected_score_max))

    medians = {c.case_id: c.median for c in cases}
    pairs = [
        PairScore(
            case_id=case.id, paired_with=case.paired_with, kind=case.kind,
            difference=medians[case.id] - medians[case.paired_with],
            within_tolerance=abs(medians[case.id] - medians[case.paired_with]) <= PAIR_TOLERANCE)
        for case in corpus.cases
        if case.paired_with and case.id in medians and case.paired_with in medians
    ]

    agreement = None
    if human:
        both = [(human[c.case_id], c.median) for c in cases if c.case_id in human]
        if both:
            differences = [abs(h - m) for h, m in both]
            agreement = HumanAgreement(
                judged_by=judged_by or "a person, not named",
                answers_compared=len(both),
                mean_absolute_difference=round(statistics.fmean(differences), 2),
                within_one_point=round(sum(d <= 1 for d in differences) / len(both), 2))

    return Findings(
        cases=cases,
        band_agreement=round(sum(c.in_band for c in cases) / len(cases), 2) if cases else 0.0,
        bands_were_assigned_by=corpus.expected_scores_judged_by,
        largest_spread=max((c.spread for c in cases), default=0),
        pairs=pairs, human=agreement)


def read_human_scores(path: Path) -> tuple[dict[str, int], str]:
    """A CSV with the columns case_id, score, judged_by. One person per file."""
    scores: dict[str, int] = {}
    judges: set[str] = set()
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not (row.get("score") or "").strip():
                continue
            score = int(row["score"])
            if not 0 <= score <= 10:
                raise ValueError(f"{row['case_id']}: a score is 0 to 10, not {score}")
            scores[row["case_id"]] = score
            judges.add((row.get("judged_by") or "").strip())
    judges.discard("")
    if len(judges) > 1:
        raise ValueError("one person per file: this one names " + ", ".join(sorted(judges)))
    return scores, next(iter(judges), "")


def render(found: Findings, run: Run) -> str:
    lines = [
        f"Interview grading, corpus {run.corpus}",
        f"{run.provider} / {run.model} / preset {run.preset}, {run.calls} calls, "
        f"usage {run.usage}",
        "",
        f"  in the expected band   {found.band_agreement:.0%}   "
        "(bands assigned by an AI assistant, not a person)",
        f"  largest spread         {found.largest_spread} point(s) across {run.repeats} repeats",
    ]
    for pair in found.pairs:
        lines.append(
            f"  {pair.case_id} vs {pair.paired_with}: {pair.difference:+.1f}"
            + ("" if pair.within_tolerance else "   OUTSIDE TOLERANCE"))
    if found.human:
        lines += ["", f"  against scores given by {found.human.judged_by}: "
                      f"mean difference {found.human.mean_absolute_difference}, "
                      f"{found.human.within_one_point:.0%} within one point, "
                      f"{found.human.answers_compared} answers"]
    lines += ["", found.caution]
    return "\n".join(lines)


# ── The live run ────────────────────────────────────────────────────────

class Refused(Exception):
    """The run was not started, and why."""


def check_may_run(corpus: InterviewCorpus, *, live: bool, max_calls: int | None,
                  repeats: int) -> None:
    from pipeline.runtime_mode import demo_mode

    needed = planned_calls(corpus, repeats)
    if not live:
        raise Refused("not asked to: pass --live to run the model")
    if max_calls is None:
        raise Refused(f"--live needs --max-calls. This plan makes {needed} calls.")
    if max_calls < needed:
        raise Refused(f"this plan makes {needed} calls and --max-calls allows {max_calls}")
    if demo_mode():
        raise Refused("SCRIVIO_DEMO is set. Demo answers are fixed, and grading "
                      "them would measure nothing.")


def _question(case: InterviewCase):
    from pipeline.schemas.models import InterviewQuestion

    return InterviewQuestion(
        id="q1", question=case.question, difficulty=case.level,
        rubric_key_points=case.rubric_key_points, model_answer=case.model_answer)


async def run_live(corpus: InterviewCorpus, *, repeats: int, preset: str) -> Run:
    from main import _anthropic_client, _resolve_provider
    from pipeline.model_config import get_model
    from pipeline.schemas.models import ArticleRequest
    from pipeline.workers.answer_evaluator_worker import evaluate_answer

    request = ArticleRequest(topic="interview-grading-eval")
    provided = _anthropic_client(request)             # raises when none is configured
    if type(provided).__name__.startswith("Mock"):
        # The canned clients answer every question the same way. A run
        # against one would be saved with scores in it and would look
        # like a result.
        raise Refused("the client is a canned one, so there is no grader to measure")
    client = _Metered(provided)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    observations: list[Observation] = []
    for case in corpus.cases:
        for repeat in range(repeats):
            before = time.monotonic()
            evaluation = await evaluate_answer(
                question=_question(case), user_answer=case.candidate_answer,
                level=case.level, client=client, preset=preset)
            observations.append(Observation(
                case_id=case.id, repeat=repeat, score=evaluation.score,
                verdict=str(evaluation.verdict),
                latency_seconds=round(time.monotonic() - before, 2),
                input_tokens=client.last_usage[0], output_tokens=client.last_usage[1]))

    reported = sum(o.input_tokens is not None for o in observations)
    usage = ("measured" if reported == len(observations)
             else "partly measured" if reported else "not reported")
    return Run(
        corpus=corpus.version, started=started, provider=_resolve_provider(request.llm_provider),
        model=get_model("evaluator", preset), preset=preset, repeats=repeats,
        calls=client.calls, usage=usage, observations=observations)


class _Metered:
    """Counts calls and keeps what the provider said about tokens. A
    subscription command-line assistant says nothing, and nothing is what
    is recorded."""

    def __init__(self, client):
        self._client, self.calls, self.last_usage = client, 0, (None, None)
        self.messages = self

    async def create(self, **kwargs):
        self.calls += 1
        response = await self._client.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        self.last_usage = (getattr(usage, "input_tokens", None),
                           getattr(usage, "output_tokens", None))
        return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", default=CURRENT)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--preset", default="balanced")
    parser.add_argument("--live", action="store_true", help="run the model")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--score", type=Path, help="work out findings from a saved run")
    parser.add_argument("--human", type=Path, help="CSV of scores a person gave")
    args = parser.parse_args(argv)
    corpus = load(args.corpus)

    if args.score:
        run = Run.model_validate_json(args.score.read_text(encoding="utf-8"))
        human, judged_by = read_human_scores(args.human) if args.human else (None, "")
        print(render(findings(corpus, run, human, judged_by), run))
        return 0

    try:
        check_may_run(corpus, live=args.live, max_calls=args.max_calls, repeats=args.repeats)
    except Refused as why:
        print(f"Plan: {len(corpus.cases)} answers, {args.repeats} repeats each, "
              f"{planned_calls(corpus, args.repeats)} calls to the grader.")
        print(f"Nothing was run: {why}")
        return 0 if not args.live else 2

    try:
        run = asyncio.run(run_live(corpus, repeats=args.repeats, preset=args.preset))
    except Refused as why:
        print(f"Nothing was run: {why}")
        return 2
    RESULTS.mkdir(exist_ok=True)
    saved = RESULTS / f"interview-grading-{run.started.replace(':', '')}.json"
    saved.write_text(json.dumps(run.model_dump(), indent=2) + "\n", encoding="utf-8")
    print(render(findings(corpus, run), run))
    print(f"\nSaved to {saved.relative_to(Path.cwd()) if saved.is_relative_to(Path.cwd()) else saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
