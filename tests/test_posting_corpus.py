"""The posting corpus keeps its own rules.

Nothing here calls a model or uses the network. The postings were
fetched once, by hand, and are read from the repository.
"""
import hashlib
import json
import re
from pathlib import Path

import pytest

from evals import collect_postings, posting_pairs_eval
from evals.posting_models import CLAIMS, Posting

CORPUS = posting_pairs_eval.CORPUS
MAY_BE_COPIED = {"USAJOBS", "TTS, U.S. General Services Administration"}


@pytest.fixture(scope="module")
def postings():
    return posting_pairs_eval.postings()


@pytest.fixture(scope="module")
def result():
    return posting_pairs_eval.evaluate()


def test_every_posting_says_where_it_came_from_and_when(postings):
    assert len(postings) == 25
    for p in postings:
        assert p.source in MAY_BE_COPIED, p.id
        assert p.source_url.startswith(("https://www.usajobs.gov/job/",
                                        "https://github.com/18F/join.tts.gsa.gov/blob/")), p.id
        assert p.collected_on == "2026-09-29", p.id
        assert p.licence and p.licence_basis, p.id
        assert "agency contact information" in p.sections_left_out or p.source.startswith("TTS")


def test_no_posting_carries_a_persons_contact_details(postings):
    for p in postings:
        assert p.personal_details_found == 0, p.id
        assert collect_postings.PERSONAL.findall(p.text) == [], p.id


def test_every_resume_is_invented_and_says_so():
    held = posting_pairs_eval.resumes()

    assert len(held) == 9
    for resume in held.values():
        assert "invented" in resume.synthetic
        assert resume.resume.basics.email.endswith("@example.com")
        assert resume.resume.basics.phone.startswith("+1 555 010 ")
        assert len(resume.honest_rewordings) == 4


def test_no_resume_is_the_owners():
    """By what can be checked from here: the name the repository's
    commits are made under appears in none of them."""
    import subprocess

    owner = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True,
                           cwd=CORPUS).stdout.strip().replace("-", " ").casefold()
    for path in (CORPUS / "resumes").glob("*.json"):
        text = path.read_text(encoding="utf-8").casefold()
        for part in owner.split():
            assert len(part) < 4 or part not in text, (path.name, part)


def test_every_posting_is_paired_with_a_resume_that_exists(postings):
    paired, held = posting_pairs_eval.pairs(), posting_pairs_eval.resumes()

    assert set(paired) == {p.id for p in postings}
    assert set(paired.values()) <= set(held)


def test_the_split_is_the_one_the_hash_gives_and_nobody_chose(postings):
    for p in postings:
        assert p.split == collect_postings.split_of(p.id), p.id
    held_out = [p for p in postings if p.split == "held_out"]
    assert len(held_out) == 8 and len(postings) - len(held_out) == 17


def test_the_annotations_say_who_made_them():
    notes = posting_pairs_eval.annotations()

    assert "AI assistant" in notes.annotated_by
    assert "Not reviewed by a person" in notes.annotated_by
    assert set(CLAIMS) < set(notes.kinds)


def test_the_held_out_postings_give_up_totals_and_nothing_else(result, capsys):
    report, _ = result
    held_out = next(s for s in report.splits if s.split == "held_out")
    tuning = next(s for s in report.splits if s.split == "tuning")

    assert held_out.which_names_were_missed == []
    assert held_out.which_words_were_mistaken == []
    assert held_out.which_claims_were_retained == []
    assert held_out.which_rewordings_were_reverted == []
    assert held_out.names_missed > 0 and held_out.ordinary_words_taken_for_names > 0
    assert len(tuning.which_names_were_missed) == tuning.names_missed

    assert posting_pairs_eval.main(["--split", "held_out", "--detail"]) == 2
    assert posting_pairs_eval.main(["--detail"]) == 2
    assert "not shown" in capsys.readouterr().err


def test_the_two_kinds_of_mistake_are_reported_apart(result):
    report, outcomes = result
    text = posting_pairs_eval.render(report)

    assert "UNSUPPORTED CLAIMS RETAINED" in text
    assert "SUPPORTED OR HONEST LINES REVERTED" in text
    assert "No model wrote anything here" in text
    assert {o.family for o in outcomes} == {
        "unsupported", "supported", "ordinary_word", "honest_rewording"}
    assert {o.when for o in outcomes} == {"first tailoring", "later edit"}


def test_the_same_posting_gives_the_same_result_twice(postings):
    notes, held = posting_pairs_eval.annotations(), posting_pairs_eval.resumes()
    paired = posting_pairs_eval.pairs()
    posting = postings[0]

    once = posting_pairs_eval.outcomes_for(posting, held[paired[posting.id]], notes)
    again = posting_pairs_eval.outcomes_for(posting, held[paired[posting.id]], notes)

    assert once and [o.model_dump() for o in once] == [o.model_dump() for o in again]


def test_the_result_on_record_is_the_result(result):
    """The numbers recorded beside the corpus are what the corpus and
    the guard give. If the guard changes, a new result is recorded beside the
    old one, and this test is pointed at it."""
    report, _ = result
    recorded = json.loads((posting_pairs_eval.CORPUS / "results"
                           / "2026-09-29-guard-a3c1a9d.json").read_text(encoding="utf-8"))

    def figures(splits):
        return [{k: v for k, v in s.items() if not k.startswith("which_")} for s in splits]

    assert figures(recorded["splits"]) == figures(report.model_dump()["splits"])
    for split in recorded["splits"]:
        if split["split"] == "held_out":
            assert all(split[k] == [] for k in split if k.startswith("which_"))


def test_a_supported_name_is_never_reverted(result):
    """The one family with no mistakes in it, on either set. Held here
    so that a change to the guard that breaks it is seen."""
    _, outcomes = result

    assert [o for o in outcomes if o.family == "supported" and o.wrong] == []


# ── The collector, on pages made for the test ─────────────────────────

PAGE = """<html><body>
<h1>IT Specialist (EXAMPLE)</h1>
<span class="usajobs-joa-banner__dept">Department of Examples</span>
<div id="joa-summary"><h2>Summary</h2><p>This position builds examples.</p></div>
<div id="joa-duties"><h2>Duties</h2><button>Help</button><ul>
  <li>Develops services in Python&nbsp;and Go.</li><li>Operates AWS.</li></ul></div>
<div id="joa-requirements"><h2>Requirements</h2><h3>Conditions of employment</h3>
  <p>Must pass a background check.</p><h3>Qualifications</h3><p>One year of experience.</p>
  <h3>Additional information</h3><p>Not kept.</p></div>
<div id="agencycontact"><p>Jo Example, jo.example@agency.example, (555) 010-0199</p></div>
</body></html>"""


def test_the_collector_keeps_the_job_and_leaves_out_the_person():
    made = collect_postings.record("usajobs", "123456789", PAGE.encode(), "2026-01-01")

    Posting.model_validate({**made, "text": made["text"] + " " * 500})
    assert made["id"] == "usajobs-123456789"
    assert made["title"] == "IT Specialist (EXAMPLE)"
    assert made["publisher"] == "Department of Examples"
    assert "Develops services in Python and Go." in made["text"]
    assert "One year of experience." in made["text"]
    for left_out in ("Help", "background check", "Not kept", "Jo Example", "agency.example"):
        assert left_out not in made["text"], left_out
    assert made["personal_details_found"] == 0
    assert made["raw_sha256"] == hashlib.sha256(PAGE.encode()).hexdigest()


def test_the_collector_counts_contact_details_that_got_into_the_text():
    page = PAGE.replace("This position builds examples.",
                        "Questions to jo.example@agency.example or 555-010-0199.")

    made = collect_postings.record("usajobs", "123456789", page.encode(), "2026-01-01")

    assert made["personal_details_found"] == 2


def test_the_collector_fetches_nothing_unless_told_to(monkeypatch, capsys):
    import urllib.request

    def must_not(*_a, **_k):
        raise AssertionError("the collector reached for the network")

    monkeypatch.setattr(urllib.request, "urlopen", must_not)

    assert collect_postings.main([]) == 0
    assert "Nothing was fetched" in capsys.readouterr().out


def test_the_collector_refuses_a_source_whose_text_may_not_be_copied(tmp_path, monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))

    assert collect_postings.main(
        ["--fetch", "linkedin", "12345", "--into", str(tmp_path)]) == 2


def test_nothing_under_evals_uses_the_network_but_the_collector():
    root = Path(posting_pairs_eval.__file__).parent
    reaching = sorted(
        p.name for p in root.glob("*.py")
        if re.search(r"^\s*(import|from) (urllib\.request|httpx|requests|socket)\b",
                     p.read_text(encoding="utf-8"), re.M))

    assert reaching == ["collect_postings.py"]
