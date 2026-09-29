"""How the resume guard does against real postings. Offline: no model.

    python -m evals.posting_pairs_eval                       # both splits, aggregates
    python -m evals.posting_pairs_eval --split tuning --detail
    python -m evals.posting_pairs_eval --json report.json

WHAT THIS IS, AND IS NOT
------------------------
The postings are real and public. The resumes are invented. The rewrites
are CONSTRUCTED, by rule, from the two: a name from the posting is put
into a line of the resume, or a word from the posting is, or the line is
reworded by hand. No model wrote any of them.

So this measures what the guard does WHEN a rewrite has a given fault or
a given virtue. It does not measure how often a real model produces
either. A rate here is a rate over constructed cases. It is not an error
rate for the product, and must not be reported as one.

TWO KINDS OF MISTAKE, COUNTED APART
-----------------------------------
  retained   an unsupported claim was put in, and it is still there
  reverted   a supported or honest line was changed or removed

They trade against each other. A guard that refuses everything retains
nothing and reverts everything, so neither number means anything alone.

THE HELD-OUT POSTINGS
---------------------
A third of the postings, chosen by a hash of their identifiers before
any result was seen. For those, only totals are printed: which words
and which names went wrong is not shown, so that the guard cannot be
adjusted to them. --detail is refused for that split.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from evals.posting_models import (
    CLAIMS, Annotations, Outcome, Posting, Report, SplitReport, SyntheticResume, Tally,
)
from pipeline.schemas.models import StructuredResume, TailoredResume
from pipeline.workers.resume_fact_guard import posting_names, validate_model_output

CORPUS = Path(__file__).resolve().parent / "corpus" / "postings-v1"
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[./-][A-Za-z0-9+#]+)*")
GRADE = re.compile(r"^[a-z]{1,3}-?\d[\d/-]*\d?$|^\d+[a-z]$", re.I)
MOST_NAMES_PER_POSTING = 8
MOST_WORDS_PER_POSTING = 40


# ── Reading the corpus ──────────────────────────────────────────────────────

def postings() -> list[Posting]:
    return [Posting.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted((CORPUS / "postings").glob("*.json"))]


def resumes() -> dict[str, SyntheticResume]:
    found = [SyntheticResume.model_validate_json(p.read_text(encoding="utf-8"))
             for p in sorted((CORPUS / "resumes").glob("*.json"))]
    return {r.id: r for r in found}


def annotations() -> Annotations:
    return Annotations.model_validate_json(
        (CORPUS / "annotations.json").read_text(encoding="utf-8"))


def pairs() -> dict[str, str]:
    return json.loads((CORPUS / "pairs.json").read_text(encoding="utf-8"))


# ── Words ───────────────────────────────────────────────────────────────────

def pieces(text: str) -> list[str]:
    """Every word, and each half of a word joined by a slash or hyphen."""
    found: list[str] = []
    for m in WORD.finditer(text):
        whole = m.group().strip(".-/")
        parts = [p for p in re.split(r"[/-]", whole) if p]
        found.extend([whole] if len(parts) < 2 else [whole, *parts])
    return found


def folded(text: str) -> set[str]:
    return {p.casefold() for p in pieces(text)}


def text_of(resume: StructuredResume) -> str:
    out: list[str] = []

    def walk(value) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(resume.model_dump())
    return "\n".join(out)


def in_the_posting(posting: Posting, notes: Annotations):
    """(names as the posting writes them, in order; ordinary capitalised words)."""
    names: dict[str, str] = {}
    ordinary: dict[str, str] = {}
    for piece in pieces(posting.text):
        key = piece.casefold()
        shaped = any(c.isupper() for c in piece) or any(c.isdigit() for c in piece)
        if key in notes.names:
            note = notes.names[key]
            if note.also_a_word and not shaped:
                continue                       # "go", as a verb
            names.setdefault(key, piece)
        elif GRADE.match(piece):
            continue                           # a pay grade: a name, and not a claim
        elif shaped and piece.isalpha() and len(piece) > 2:
            ordinary.setdefault(key, piece)
    return names, ordinary


# ── The constructed rewrites ────────────────────────────────────────────────

def _changed(resume: StructuredResume, job: int, line: int, text: str) -> StructuredResume:
    copy = resume.model_copy(deep=True)
    copy.work[job].highlights[line] = text
    return copy


def _guarded(resume, rewritten, posting_text, *, later: bool) -> TailoredResume:
    baseline = (TailoredResume(resume=resume.model_copy(deep=True), changes=[], warnings=[])
                if later else None)
    return validate_model_output(
        resume.model_copy(deep=True),
        TailoredResume(resume=rewritten, changes=[], warnings=[]),
        baseline=baseline, jd_text=posting_text)


def outcomes_for(posting: Posting, held: SyntheticResume, notes: Annotations) -> list[Outcome]:
    resume = held.resume
    on_the_resume = folded(text_of(resume))
    names, ordinary = in_the_posting(posting, notes)
    lines = [(j, i, h) for j, w in enumerate(resume.work) for i, h in enumerate(w.highlights)]
    found: list[Outcome] = []

    def lower_first(text: str) -> str:
        return text[0].lower() + text[1:]

    def run(family, form, what, line_at, text, *, kind="", word=False, wrong_if):
        job, line, _ = lines[line_at % len(lines)]
        for later in (False, True):
            out = _guarded(resume, _changed(resume, job, line, text), posting.text, later=later)
            kept = out.resume.work[job].highlights
            found.append(Outcome(
                posting=posting.id, split=posting.split, family=family, form=form,
                when="later edit" if later else "first tailoring", what=what, kind=kind,
                also_a_word=word, wrong=wrong_if(out, kept, text)))

    def still_there(name):
        return lambda out, kept, text: name.casefold() in folded(text_of(out.resume))

    def not_as_written(out, kept, text):
        return text not in kept

    # An unsupported claim: a name the posting uses and the resume does not.
    unsupported = [(k, w) for k, w in names.items()
                   if notes.names[k].kind in CLAIMS and k not in on_the_resume]
    for n, (key, written) in enumerate(unsupported[:MOST_NAMES_PER_POSTING]):
        note, base = notes.names[key], lines[n % len(lines)][2]
        shared = dict(kind=note.kind, word=note.also_a_word, wrong_if=still_there(key))
        run("unsupported", "as the posting writes it", key, n, f"{base}, using {written}", **shared)
        run("unsupported", "lower case, mid-sentence", key, n, f"{base}, using {key}", **shared)
        run("unsupported", "lower case, first word", key, n,
            f"{key} used throughout: {lower_first(base)}", **shared)
        run("unsupported", "upper case, mid-sentence", key, n,
            f"{base}, using {key.upper()}", **shared)

    # A supported claim: a name that is on the resume, written into another line.
    own = [p for p in dict.fromkeys(pieces(text_of(resume)))
           if p.casefold() in notes.names and notes.names[p.casefold()].kind in CLAIMS]
    for n, written in enumerate(own[:MOST_NAMES_PER_POSTING]):
        base = lines[(n + 1) % len(lines)][2]
        for form, text in (("as the resume writes it", f"{base}, using {written}"),
                           ("lower case", f"{base}, using {written.lower()}")):
            if written.casefold() in folded(base):
                continue
            run("supported", form, written.casefold(), n + 1, text,
                kind=notes.names[written.casefold()].kind, wrong_if=not_as_written)

    # An ordinary word of the posting's, which the posting happens to capitalise.
    words = [(k, w) for k, w in ordinary.items() if k not in on_the_resume]
    for n, (key, written) in enumerate(words[:MOST_WORDS_PER_POSTING]):
        base = lines[n % len(lines)][2]
        run("ordinary_word", "lower case, mid-sentence", key, n,
            f"{base}, with {key} in mind", wrong_if=not_as_written)
        run("ordinary_word", "capitalised, first word", key, n,
            f"{key.capitalize()} mattered here: {lower_first(base)}", wrong_if=not_as_written)

    # A line reworded by hand, honestly.
    for reworded in held.honest_rewordings:
        m = re.fullmatch(r"work\[(\d+)\]\.highlights\[(\d+)\]", reworded.where)
        at = next(n for n, (j, i, _) in enumerate(lines) if (j, i) == (int(m[1]), int(m[2])))
        run("honest_rewording", "reworded by hand", reworded.where, at, reworded.line,
            wrong_if=not_as_written)
    return found


# ── Adding up ───────────────────────────────────────────────────────────────

def _tally(outcomes, **where) -> Tally:
    chosen = [o for o in outcomes if all(getattr(o, k) == v for k, v in where.items())]
    return Tally(cases=len(chosen), wrong=sum(o.wrong for o in chosen))


def evaluate() -> tuple[Report, list[Outcome]]:
    notes, held, paired = annotations(), resumes(), pairs()
    everything: list[Outcome] = []
    splits: list[SplitReport] = []
    for split in ("tuning", "held_out"):
        chosen = [p for p in postings() if p.split == split]
        outcomes: list[Outcome] = []
        annotated = plain = 0
        missed: list[str] = []
        mistaken: list[str] = []
        for posting in chosen:
            names, ordinary = in_the_posting(posting, notes)
            guessed = posting_names(posting.text)
            claims = [k for k in names if notes.names[k].kind in CLAIMS]
            annotated += len(claims)
            missed += [f"{posting.id}: {names[k]}" for k in claims if k not in guessed]
            plain += len(ordinary)
            mistaken += [f"{posting.id}: {w}" for k, w in ordinary.items() if k in guessed]
            outcomes += outcomes_for(posting, held[paired[posting.id]], notes)
        unsupported = [o for o in outcomes if o.family == "unsupported"]
        shown = split == "tuning"
        splits.append(SplitReport(
            split=split, postings=len(chosen), names_annotated=annotated,
            names_missed=len(missed), ordinary_words=plain,
            ordinary_words_taken_for_names=len(mistaken),
            which_names_were_missed=missed if shown else [],
            which_words_were_mistaken=mistaken if shown else [],
            which_claims_were_retained=sorted({
                f"{o.what} ({o.form})" for o in unsupported if o.wrong}) if shown else [],
            which_rewordings_were_reverted=sorted({
                f"{o.posting}: {o.what}" for o in outcomes
                if o.family == "honest_rewording" and o.wrong}) if shown else [],
            unsupported_retained=_tally([o for o in unsupported if not o.also_a_word]),
            unsupported_retained_by_form={
                form: _tally([o for o in unsupported if not o.also_a_word], form=form)
                for form in dict.fromkeys(o.form for o in unsupported)},
            unsupported_retained_names_that_are_also_words=_tally(
                [o for o in unsupported if o.also_a_word]),
            supported_reverted=_tally(outcomes, family="supported"),
            ordinary_word_reverted=_tally(outcomes, family="ordinary_word"),
            honest_rewording_reverted=_tally(outcomes, family="honest_rewording")))
        everything += outcomes
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, cwd=CORPUS, timeout=10).stdout.strip() or "unknown"
    except OSError:
        commit = "unknown"
    return Report(
        corpus="postings-v1", guard_commit=commit, splits=splits,
        what_this_is="Constructed rewrites against real public postings and invented resumes. "
                     "No model wrote anything here. These are rates over constructed cases "
                     "and say nothing about how often a real model makes these mistakes."), everything


def _rate(t: Tally) -> str:
    return f"{t.wrong:4} of {t.cases:4}  ({t.rate:6.1%})" if t.cases else "   none"


def together(s: SplitReport) -> Tally:
    """Every unsupported claim constructed for the split. The names that
    are also words (Go, Rust) are counted apart from the rest, because
    the guard treats them differently, and a reader who is given one of
    the two figures takes it for the whole."""
    apart = s.unsupported_retained_names_that_are_also_words
    return Tally(cases=s.unsupported_retained.cases + apart.cases,
                 wrong=s.unsupported_retained.wrong + apart.wrong)


def render(report: Report, *, detail: str | None = None) -> str:
    lines = [f"Resume guard against real postings, corpus {report.corpus}, guard at "
             f"{report.guard_commit}", "", report.what_this_is, ""]
    for s in report.splits:
        lines += [
            f"== {s.split}: {s.postings} postings",
            "",
            "  Recognising the posting's names",
            f"    names the guard did not collect         "
            f"{s.names_missed:4} of {s.names_annotated:4}",
            f"    ordinary words it took for names        "
            f"{s.ordinary_words_taken_for_names:4} of {s.ordinary_words:4}",
            "",
            "  UNSUPPORTED CLAIMS RETAINED (a claim nobody made, still on the resume)",
            f"    every constructed claim                 {_rate(together(s))}",
            "    of which",
            f"    names that are not also words           {_rate(s.unsupported_retained)}",
        ]
        lines += [f"      {form:38}  {_rate(t)}" for form, t in s.unsupported_retained_by_form.items()]
        lines += [
            f"    names that are also words               "
            f"{_rate(s.unsupported_retained_names_that_are_also_words)}",
            "",
            "  SUPPORTED OR HONEST LINES REVERTED (a line that was fine, put back)",
            f"    a name that is on the resume            {_rate(s.supported_reverted)}",
            f"    an ordinary word from the posting       {_rate(s.ordinary_word_reverted)}",
            f"    a line reworded by hand                 {_rate(s.honest_rewording_reverted)}",
            ""]
        if detail == s.split:
            for title, which in (
                    ("names not collected", s.which_names_were_missed),
                    ("ordinary words taken for names", s.which_words_were_mistaken),
                    ("unsupported claims retained", s.which_claims_were_retained),
                    ("honest rewordings reverted", s.which_rewordings_were_reverted)):
                lines += [f"  {title}:"] + [f"    {m}" for m in which] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--split", choices=["tuning", "held_out"])
    parser.add_argument("--detail", action="store_true",
                        help="list what went wrong. Tuning postings only")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    if args.detail and args.split != "tuning":
        print("--detail is for the tuning postings, with --split tuning. What went wrong in "
              "the held-out postings is not shown, so that the guard cannot be adjusted to "
              "them.", file=sys.stderr)
        return 2
    report, _ = evaluate()
    if args.split:
        report.splits = [s for s in report.splits if s.split == args.split]
    print(render(report, detail="tuning" if args.detail else None))
    if args.json:
        args.json.write_text(json.dumps(report.model_dump(), indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
