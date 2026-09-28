"""The evaluation corpus and its runners (R24).

Nothing here calls a model. The resume evaluation is deterministic. The
interview evaluation is a live run by nature, so what is tested is that
its cases are well formed and that it cannot start by accident.
"""
import json

import pytest

import main
from evals import resume_guard_eval

# Taken when this file is read, before the suite's own fixture replaces
# it with the canned client for the length of each test.
REAL_WRITER = main._anthropic_client
from evals.corpus_models import ResumeCorpus

ADVERSARIAL = {"unsupported_fact", "promotion"}


@pytest.fixture(scope="module")
def corpus() -> ResumeCorpus:
    return resume_guard_eval.load()


def unguarded(original, candidate, **_):
    """The guard, removed: what the model wrote is what is kept."""
    return candidate


def test_the_guard_behaves_as_the_corpus_records(corpus):
    report = resume_guard_eval.evaluate(corpus)

    assert [r.id for r in report.results if r.outcome == "failed"] == []
    assert [r.id for r in report.results if r.outcome == "gap_closed"] == [], \
        "a known gap now passes: clear its known_gap so it is held to the standard"
    assert report.as_recorded


def test_with_the_guard_removed_every_adversarial_case_fails(corpus):
    """A case that passes with no guard at all tests nothing. This is
    what makes the first test mean something."""
    report = resume_guard_eval.evaluate(corpus, guard=unguarded)

    passed_anyway = [
        r.id for r in report.results
        if r.category in ADVERSARIAL and r.outcome in ("held", "gap_closed")
        # This one is about the guard choosing NOT to remove something.
        and r.id not in {"new-technology-in-a-sentence"}
    ]
    assert passed_anyway == []


def test_a_weakened_guard_is_caught(corpus):
    """The regression this exists for: someone loosens the number check."""
    from pipeline.workers.resume_fact_guard import enforce_honesty, note_new_terms

    def without_the_number_check(original, candidate, *, baseline=None,
                                 user_text="", jd_text=""):
        candidate = enforce_honesty(original, candidate)
        return note_new_terms(original, candidate, baseline=baseline,
                              user_text=user_text, jd_text=jd_text)

    report = resume_guard_eval.evaluate(corpus, guard=without_the_number_check)

    failed = {r.id for r in report.results if r.outcome == "failed"}
    assert {"invented-metric", "number-moved-between-claims",
            "number-moved-between-employers", "spanish-invented-metric",
            "japanese-invented-metric"} <= failed
    assert not report.as_recorded


def test_the_runner_exits_nonzero_when_the_guard_does_not_hold(corpus, monkeypatch, capsys):
    monkeypatch.setattr(resume_guard_eval, "validate_model_output", unguarded)
    monkeypatch.setattr(
        resume_guard_eval, "evaluate",
        lambda c, guard=unguarded, _real=resume_guard_eval.evaluate: _real(c, guard))

    assert resume_guard_eval.main([]) == 1
    assert "Failed:" in capsys.readouterr().out


def test_the_runner_exits_zero_and_writes_a_report(tmp_path, capsys):
    out = tmp_path / "report.json"

    assert resume_guard_eval.main(["--json", str(out)]) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["cases"] == report["held"] + report["gaps"]
    assert "known gap" in capsys.readouterr().out


def test_a_clean_exit_is_not_allowed_to_read_as_everything_passing(capsys):
    """The follow-up review pointed out that it could be read that way."""
    assert resume_guard_eval.main([]) == 0
    said = capsys.readouterr().out.strip().splitlines()[-1]

    assert said.startswith("Result: the guard does what is expected of it in ")
    assert "It does NOT catch the other" in said


def test_strict_is_red_while_any_gap_remains():
    assert resume_guard_eval.main(["--strict"]) == 1


def test_the_first_corpus_is_as_it_was_when_results_were_recorded_against_it():
    v1 = resume_guard_eval.evaluate(resume_guard_eval.load("v1"))

    assert (v1.cases, v1.held, v1.failed, v1.gaps) == (38, 35, 0, 3)


def test_the_second_corpus_keeps_every_case_of_the_first_unchanged():
    v1, v2 = resume_guard_eval.load("v1"), resume_guard_eval.load("v2")
    carried = {c.id: c for c in v2.cases}

    for case in v1.cases:
        kept = carried[case.id]
        assert kept.baseline is None
        assert kept.model_dump(exclude={"baseline"}) == case.model_dump(), case.id


def test_the_second_corpus_has_the_cases_the_follow_up_review_asked_for(corpus):
    ids = {c.id for c in corpus.cases}

    assert {"edit-count-changes-what-it-counts", "edit-percentage-moved-in-place",
            "edit-amount-moved-in-place", "edit-percentage-reworded",
            "edit-amount-reworded", "user-rejected-the-number"} <= ids
    assert sum(c.baseline is not None for c in corpus.cases) == 15


def test_without_the_fix_the_edit_cases_fail(corpus, monkeypatch):
    """The guard as it was: a figure already on the line was accepted
    whatever it now counted."""
    from pipeline.workers import resume_fact_guard as guard

    real_quantities = guard.quantities_in

    def as_it_was(text, claims, *, original, prior_text="", allowance=None):
        prior = {q.key for q in real_quantities(prior_text, source=True)}
        return [q for q in guard.__dict__["_unsupported_now"](
                    text, claims, original=original, prior_text="", allowance=allowance)
                if q.key not in prior]

    monkeypatch.setitem(guard.__dict__, "_unsupported_now", guard.unsupported_quantities)
    monkeypatch.setattr(guard, "unsupported_quantities", as_it_was)

    report = resume_guard_eval.evaluate(corpus)

    failed = {r.id for r in report.results if r.outcome == "failed"}
    assert {"edit-count-changes-what-it-counts", "edit-percentage-moved-in-place",
            "edit-amount-moved-in-place", "edit-figure-the-user-filled-in-is-moved"} <= failed


def test_every_category_the_review_asked_for_has_cases(corpus):
    found = {c.category for c in corpus.cases}

    assert found == {
        "unsupported_fact", "missing_information", "promotion", "reordered_entries",
        "format_variation", "language_variation", "legitimate_edit"}


def test_case_ids_are_unique_and_every_gap_gives_its_reason(corpus):
    ids = [c.id for c in corpus.cases]

    assert len(ids) == len(set(ids))
    assert all(len(c.known_gap) > 40 for c in corpus.cases if c.known_gap)


def test_nobody_in_the_corpus_could_be_a_real_person(corpus):
    for case in corpus.cases:
        basics = case.original.basics
        assert basics.email.endswith("@example.com"), case.id
        assert "555" in basics.phone, case.id


def test_the_corpus_says_who_wrote_it(corpus):
    assert "AI assistant" in corpus.written_by
    assert "Not reviewed by a recruiter" in corpus.written_by


# ── Figures as other countries write them ─────────────────────────────
# Found by writing the language cases: "12,5 %" came back as
# "12,[METRIC] %" and "2.000.000" as "[METRIC].000". Both figures were
# real. The guard read each as two numbers and replaced the half it
# could not find.

@pytest.mark.parametrize("written, value", [
    ("12,5", 12.5), ("12.5", 12.5), ("2.000.000", 2_000_000), ("2,000,000", 2_000_000),
    ("1.234,56", 1234.56), ("1,234.56", 1234.56), ("1,234", 1234), ("0,5", 0.5),
    ("2 000 000", 2_000_000), ("40", 40),
])
def test_a_figure_is_read_whole_however_it_is_written(written, value):
    from pipeline.workers.resume_fact_guard import quantities_in

    found = quantities_in(f"reduced it by {written} units")

    assert [q.text for q in found] == [written]
    assert found[0].key[1] == pytest.approx(value)


def test_a_percent_sign_after_a_space_belongs_to_the_figure():
    from pipeline.workers.resume_fact_guard import quantities_in

    (found,) = quantities_in("un 12,5 % al año")

    assert found.text == "12,5 %" and found.key[2] == "%"


def test_a_list_of_versions_is_still_a_list():
    from pipeline.workers.resume_fact_guard import quantities_in

    assert [q.key[1] for q in quantities_in("Java 8,11,17")] == [8, 11, 17]
    assert [q.key[1] for q in quantities_in("Java 8, 11 and 17")] == [8, 11, 17]


# ── Interview grading: a live run by nature ───────────────────────────
# What is tested is the harness: that it cannot start by accident, that
# it records what it was told and nothing it was not, and that its sums
# are right. None of this is a result about grading.

from evals import interview_grading_eval as grading  # noqa: E402


@pytest.fixture(scope="module")
def answers():
    return grading.load()


def a_run(scores: dict[str, list[int]], **changes) -> grading.Run:
    observations = [
        grading.Observation(case_id=case, repeat=i, score=s, latency_seconds=1.0)
        for case, given in scores.items() for i, s in enumerate(given)]
    return grading.Run(**{
        "corpus": "v1", "started": "2026-01-01T00:00:00Z", "provider": "test",
        "model": "test", "preset": "balanced", "repeats": 3, "calls": len(observations),
        "usage": "not reported", "observations": observations, **changes})


def test_the_interview_cases_say_their_bands_are_not_human_judgments(answers):
    assert "NOT human judgments" in answers.expected_scores_judged_by
    assert "AI assistant" in answers.written_by


def test_the_interview_cases_cover_what_the_review_asked_for(answers):
    kinds = {c.kind for c in answers.cases}

    assert {"long_and_empty", "short_and_complete", "confident_and_wrong",
            "hedged_and_correct", "instructs_the_grader", "buzzwords", "empty"} <= kinds


def test_every_band_and_every_pair_in_the_cases_is_coherent(answers):
    by_id = {c.id: c for c in answers.cases}

    for case in answers.cases:
        assert case.expected_score_min <= case.expected_score_max, case.id
        assert all(0 <= i < len(case.rubric_key_points) for i in case.points_covered), case.id
        if case.paired_with:
            assert case.paired_with in by_id, case.id
            assert by_id[case.paired_with].question == case.question, case.id


def test_the_answer_that_instructs_the_grader_is_otherwise_its_pair(answers):
    by_id = {c.id: c for c in answers.cases}
    told = next(c for c in answers.cases if c.kind == "instructs_the_grader")
    plain = by_id[told.paired_with]

    assert told.candidate_answer.startswith(plain.candidate_answer)
    assert (told.expected_score_min, told.expected_score_max) == (
        plain.expected_score_min, plain.expected_score_max)


@pytest.mark.parametrize("arguments, said", [
    ([], "pass --live"),
    (["--live"], "needs --max-calls"),
    (["--live", "--max-calls", "5"], "allows 5"),
])
def test_nothing_runs_without_being_asked_and_told_how_much(
        arguments, said, monkeypatch, capsys):
    def must_not_run(*_, **__):
        raise AssertionError("the model was about to be called")
    monkeypatch.setattr(grading, "run_live", must_not_run)

    status = grading.main(arguments)

    shown = capsys.readouterr().out
    assert "36 calls" in shown and "Nothing was run" in shown and said in shown
    assert status == (2 if "--live" in arguments else 0)


def test_demo_mode_is_refused_because_grading_canned_answers_measures_nothing(
        answers, monkeypatch):
    monkeypatch.setenv("SCRIVIO_DEMO", "1")

    with pytest.raises(grading.Refused, match="SCRIVIO_DEMO"):
        grading.check_may_run(answers, live=True, max_calls=100, repeats=3)


def test_with_no_provider_a_live_run_fails_and_does_not_fall_back_to_a_mock(
        answers, monkeypatch):
    import asyncio

    from pipeline.runtime_mode import ProviderUnavailable

    monkeypatch.setattr(main, "_anthropic_client", REAL_WRITER)
    monkeypatch.setenv("LLM_CLI", "qwen")            # a CLI that is not installed here
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)

    with pytest.raises(ProviderUnavailable):
        asyncio.run(grading.run_live(answers, repeats=1, preset="balanced"))


def test_a_canned_client_is_refused_and_nothing_is_saved(answers, monkeypatch, tmp_path, capsys):
    """Found by the test above, which first passed for the wrong reason:
    the suite's canned client answered, and the harness recorded its
    scores as it would have recorded a grader's."""
    monkeypatch.setattr(grading, "RESULTS", tmp_path / "results")
    # The suite's fixture has already put the canned client in place.

    status = grading.main(["--live", "--max-calls", "36"])

    assert status == 2
    assert "canned" in capsys.readouterr().out
    assert not (tmp_path / "results").exists()


def test_usage_the_provider_did_not_report_is_recorded_as_not_reported(
        answers, monkeypatch):
    """The harness, driven by a stand-in. The scores mean nothing."""
    import asyncio
    from types import SimpleNamespace

    class StandIn:
        def __init__(self):
            self.messages = self

        async def create(self, **_):
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={
                "score": 5, "verdict": "shallow", "strengths": [], "gaps": ["x"],
                "misconceptions": [], "suggestions": [], "section_pointers": [],
                "needs_followup": False, "followup_question": ""})])

    monkeypatch.setattr(main, "_anthropic_client", lambda request: StandIn())
    monkeypatch.setattr(main, "_resolve_provider", lambda requested="auto": "stand-in")

    run = asyncio.run(grading.run_live(answers, repeats=2, preset="balanced"))

    assert run.calls == len(answers.cases) * 2 == len(run.observations)
    assert run.usage == "not reported"
    assert all(o.input_tokens is None and o.output_tokens is None for o in run.observations)
    assert run.provider == "stand-in"


def test_findings_report_bands_spread_and_pairs(answers):
    run = a_run({
        "rebalance-complete": [9, 10, 9],
        "rebalance-short-and-complete": [6, 6, 5],       # marked down for being short
        "rebalance-partial": [4, 4, 4],
        "rebalance-instructs-the-grader": [10, 10, 10],  # did as it was told
        "rebalance-empty": [0, 1, 3],
    })

    found = grading.findings(answers, run)
    by_id = {c.case_id: c for c in found.cases}
    pairs = {p.case_id: p for p in found.pairs}

    assert by_id["rebalance-complete"].in_band
    assert not by_id["rebalance-short-and-complete"].in_band
    assert by_id["rebalance-empty"].spread == 3 == found.largest_spread
    assert found.band_agreement == 0.6
    assert pairs["rebalance-short-and-complete"].difference == -3
    assert not pairs["rebalance-short-and-complete"].within_tolerance
    assert pairs["rebalance-instructs-the-grader"].difference == 6
    assert not pairs["rebalance-instructs-the-grader"].within_tolerance
    assert "not a person" in grading.render(found, run)
    assert "cannot show that grading is right" in grading.render(found, run)


def test_scores_a_person_gave_are_compared_separately_and_by_name(answers, tmp_path):
    sheet = tmp_path / "scores.csv"
    sheet.write_text(
        "case_id,score,judged_by\n"
        "rebalance-complete,9,R. Example\n"
        "rebalance-partial,6,R. Example\n"
        "rebalance-empty,,R. Example\n", encoding="utf-8")
    run = a_run({"rebalance-complete": [9], "rebalance-partial": [4], "rebalance-empty": [0]})

    human, judged_by = grading.read_human_scores(sheet)
    found = grading.findings(answers, run, human, judged_by)

    assert found.human.judged_by == "R. Example"
    assert found.human.answers_compared == 2, "a blank row is not a score of zero"
    assert found.human.mean_absolute_difference == 1.0
    assert found.human.within_one_point == 0.5
    assert "R. Example" in grading.render(found, run)


def test_a_sheet_from_two_people_is_refused(tmp_path):
    sheet = tmp_path / "scores.csv"
    sheet.write_text("case_id,score,judged_by\na,5,One\nb,6,Two\n", encoding="utf-8")

    with pytest.raises(ValueError, match="one person per file"):
        grading.read_human_scores(sheet)


def test_the_template_for_human_scores_lists_every_case_and_no_scores(answers):
    import csv

    path = grading.CORPORA / "v1" / "human_scores_template.csv"
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert [r["case_id"] for r in rows] == [c.id for c in answers.cases]
    assert all(r["score"] == "" and r["judged_by"] == "" for r in rows)
