"""What a model adds without support does not reach a finished export (F03).

The guard used to keep a line that named something new and attach a note
asking the candidate to check it. The export gate looked only for
[METRIC], so the note could be ignored and the resume downloaded as
finished. An amber note that nobody has to answer is not a confirmation.

Until the document can record what the candidate has confirmed (approval
request A1), the rule is the conservative one: a line in which the MODEL
names something new is put back as it was. What the candidate types, in
the line or in their instruction, is theirs and stays.
"""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import server
from pipeline.schemas.models import ResumeDoc, StructuredResume, TailoredResume
from pipeline.workers.resume_fact_guard import validate_model_output
from pipeline.workers.resume_studio_worker import render_markdown, run_ats_checks

JD = ("Senior Platform Engineer.\n\nRequirements:\n- Kubernetes in production\n"
      "- Terraform\n- Python services\n")

ORIGINAL = {
    "basics": {"name": "Sam Okafor", "label": "Software Engineer",
               "email": "sam@example.com", "phone": "+1 555 010 9999",
               "location": "Denver, CO",
               "summary": "Software engineer who builds internal platforms."},
    "work": [{"name": "Initrode", "position": "Software Engineer",
              "startDate": "Feb 2020", "endDate": "Present", "summary": "",
              "highlights": ["Maintained 12 services behind the internal gateway",
                             "Mentored engineers joining the platform team",
                             "Consolidated build clusters to reduce costs"]}],
    "education": [{"institution": "Colorado State University", "area": "Computer Science",
                   "studyType": "BS", "startDate": "2014", "endDate": "2018", "score": ""}],
    "skills": [{"name": "Languages", "keywords": ["Python"]}],
    "projects": [], "certificates": [],
}
LINES = ORIGINAL["work"][0]["highlights"]


class ScriptedClient:
    """Returns the resume it was given, and the warnings it was given.
    What a model says about warnings is part of what is being tested:
    it may drop them, keep them word for word, or reword them."""

    def __init__(self, resume, warnings=()):
        self.resume, self.warnings, self.messages = resume, list(warnings), self

    async def create(self, **kwargs):
        if kwargs.get("tools"):
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use",
                input={"resume": self.resume, "changes": [],
                       "warnings": list(self.warnings)})])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="")])


def guarded(after: dict, *, before: dict | None = None, user_text: str = "",
            original: dict = ORIGINAL) -> TailoredResume:
    return validate_model_output(
        StructuredResume.model_validate(original),
        TailoredResume(resume=StructuredResume.model_validate(after), changes=[], warnings=[]),
        baseline=(TailoredResume(resume=StructuredResume.model_validate(before),
                                 changes=[], warnings=[]) if before else None),
        user_text=user_text, jd_text=JD)


def with_line(index: int, text: str, base: dict = ORIGINAL) -> dict:
    changed = copy.deepcopy(base)
    changed["work"][0]["highlights"][index] = text
    return changed


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    scripted = {}
    monkeypatch.setattr(
        server, "_client_for_session",
        lambda *a, **k: (ScriptedClient(scripted["resume"], scripted.get("warnings", ())),
                         "balanced"))
    original = StructuredResume.model_validate(ORIGINAL)
    doc = ResumeDoc(
        resume_id="20260101-000000-abc123", original_text=render_markdown(original),
        structured=original, jd_text=JD,
        report=run_ats_checks(render_markdown(original), JD, original))
    server._save_resume_doc(doc)
    return TestClient(server.app), doc.resume_id, scripted


def finished(client, rid, fmt="md"):
    return client.get(f"/resumes/{rid}/download?version=tailored&fmt={fmt}")


# ── The reproduction ──────────────────────────────────────────────────

def test_the_case_from_the_follow_up_review(api):
    client, rid, scripted = api
    scripted["resume"] = with_line(2, "Built Kubernetes clusters.")

    assert client.post(f"/resumes/{rid}/tailor").status_code == 200
    sent = finished(client, rid)

    assert sent.status_code == 200
    assert "Kubernetes" not in sent.text
    assert LINES[2] in sent.text


@pytest.mark.parametrize("fmt", ["md", "json", "docx", "pdf"])
def test_no_format_of_the_finished_export_carries_what_the_model_added(api, fmt):
    client, rid, scripted = api
    scripted["resume"] = with_line(2, "Consolidated build clusters on Kubernetes with Terraform")
    client.post(f"/resumes/{rid}/tailor")

    saved = client.get(f"/resumes/{rid}").json()["tailored"]["resume"]

    assert finished(client, rid, fmt).status_code == 200
    assert "Kubernetes" not in str(saved) and "Terraform" not in str(saved)


# ── At the guard ──────────────────────────────────────────────────────

def test_on_first_tailoring_the_line_goes_back_to_the_one_it_came_from():
    out = guarded(with_line(2, "Consolidated build clusters on Kubernetes to reduce costs"))

    assert out.resume.work[0].highlights == LINES
    assert any("Kubernetes" in w and "work[0].highlights[2]" in w for w in out.warnings)


def test_the_warning_says_how_to_put_it_back_if_it_is_true():
    out = guarded(with_line(2, "Consolidated build clusters on Kubernetes to reduce costs"))

    said = next(w for w in out.warnings if "Kubernetes" in w)
    assert "yourself" in said or "your own words" in said


def test_the_lines_around_it_keep_their_honest_rewording():
    changed = with_line(2, "Consolidated build clusters on Kubernetes to reduce costs")
    changed["work"][0]["highlights"][1] = "Mentored engineers who joined the platform team"

    out = guarded(changed)

    assert out.resume.work[0].highlights[1] == "Mentored engineers who joined the platform team"
    assert out.resume.work[0].highlights[2] == LINES[2]


def test_when_bullets_were_reordered_the_right_original_comes_back():
    changed = copy.deepcopy(ORIGINAL)
    changed["work"][0]["highlights"] = [
        "Consolidated build clusters on Kubernetes to reduce costs",
        "Maintained 12 services behind the internal gateway",
        "Mentored engineers joining the platform team"]

    out = guarded(changed)

    assert out.resume.work[0].highlights == [LINES[2], LINES[0], LINES[1]]


def test_a_bullet_the_model_added_from_nothing_is_removed():
    changed = copy.deepcopy(ORIGINAL)
    changed["work"][0]["highlights"].append("Ran Terraform pipelines across three regions")

    out = guarded(changed)

    assert out.resume.work[0].highlights == LINES
    assert any("Terraform" in w for w in out.warnings)


def test_none_gets_through_by_being_the_seventh():
    """The note was raised for the first six terms. The rest were kept
    and nothing was said."""
    named = ["Kubernetes", "Terraform", "Ansible", "Jenkins", "Prometheus",
             "Grafana", "Istio", "Consul"]
    changed = copy.deepcopy(ORIGINAL)
    changed["work"][0]["highlights"] = [
        f"Maintained 12 services behind the internal gateway on {named[0]} and {named[1]}",
        f"Mentored engineers joining the platform team on {named[2]}, {named[3]} and {named[4]}",
        f"Consolidated build clusters with {named[5]}, {named[6]} and {named[7]} to reduce costs"]

    out = guarded(changed)

    text = render_markdown(out.resume)
    assert [n for n in named if n in text] == []


def test_the_headline_and_the_summary_are_held_to_it_too():
    changed = copy.deepcopy(ORIGINAL)
    changed["basics"]["label"] = "Kubernetes Platform Engineer"
    changed["basics"]["summary"] = "Software engineer who builds internal platforms on Kubernetes."

    out = guarded(changed)

    assert out.resume.basics.label == "Software Engineer"
    assert out.resume.basics.summary == ORIGINAL["basics"]["summary"]


def test_a_term_already_on_the_resume_may_be_used_in_a_line():
    out = guarded(with_line(0, "Maintained 12 Python services behind the internal gateway"))

    assert "Python services" in out.resume.work[0].highlights[0]
    assert not [w for w in out.warnings if "Python" in w]


def test_an_edit_that_names_something_new_puts_the_earlier_line_back():
    earlier = with_line(2, "Consolidated build clusters, reducing costs")

    out = guarded(with_line(2, "Consolidated Kubernetes clusters, reducing costs", earlier),
                  before=earlier)

    assert out.resume.work[0].highlights[2] == "Consolidated build clusters, reducing costs"


def test_what_the_candidate_says_in_their_own_words_is_theirs():
    out = guarded(with_line(2, "Consolidated build clusters on Kubernetes to reduce costs"),
                  before=ORIGINAL,
                  user_text="Those clusters ran on Kubernetes, I set them up")

    assert "Kubernetes" in out.resume.work[0].highlights[2]
    assert not [w for w in out.warnings if "Kubernetes" in w]


def test_what_the_candidate_refused_in_their_own_words_is_not():
    out = guarded(with_line(2, "Consolidated build clusters on Kubernetes to reduce costs"),
                  before=ORIGINAL,
                  user_text="I have never used Kubernetes, do not mention it")

    assert "Kubernetes" not in render_markdown(out.resume)


# ── Through the application ───────────────────────────────────────────

def test_the_edit_endpoint_does_not_keep_what_the_model_added(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    scripted["resume"] = with_line(2, "Consolidated Kubernetes clusters to reduce costs")

    edited = client.post(f"/resumes/{rid}/request-edit",
                         json={"instruction": "make the last bullet match the posting"})

    assert edited.status_code == 200, edited.text
    assert "Kubernetes" not in finished(client, rid).text


def test_a_term_the_candidate_vouched_for_in_the_instruction_is_exported(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    scripted["resume"] = with_line(2, "Consolidated build clusters on Kubernetes to reduce costs")

    client.post(f"/resumes/{rid}/request-edit", json={
        "instruction": "Those clusters ran on Kubernetes, I set them up. Say so."})

    assert "on Kubernetes" in finished(client, rid).text


def test_a_term_the_candidate_typed_onto_the_paper_is_exported(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")

    typed = client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [{
        "path": "work[0].highlights[2]",
        "value": "Consolidated build clusters on Kubernetes to reduce costs"}]})

    assert typed.status_code == 200, typed.text
    sent = finished(client, rid)
    assert sent.status_code == 200 and "on Kubernetes" in sent.text


def test_what_the_candidate_typed_survives_the_models_next_edit(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    typed = "Consolidated build clusters on Kubernetes to reduce costs"
    client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [{
        "path": "work[0].highlights[2]", "value": typed}]})
    scripted["resume"] = with_line(2, "Reduced costs by consolidating build clusters on Kubernetes")

    client.post(f"/resumes/{rid}/request-edit", json={"instruction": "lead with the result"})

    assert "consolidating build clusters on Kubernetes" in finished(client, rid).text


def test_undo_goes_back_to_a_version_that_was_checked_when_it_was_written(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [{
        "path": "work[0].highlights[2]",
        "value": "Consolidated build clusters on Kubernetes to reduce costs"}]})

    undone = client.post(f"/resumes/{rid}/undo-tailored")

    assert undone.status_code == 200, undone.text
    sent = finished(client, rid)
    assert sent.status_code == 200 and "Kubernetes" not in sent.text


def test_a_note_about_something_already_put_right_does_not_hold_up_the_export(api):
    """Most warnings report what was done. They ask nothing of anyone,
    and a gate that stopped for them would never open."""
    client, rid, scripted = api
    bad = with_line(2, "Reduced costs by 73% by consolidating build clusters on Kubernetes")
    bad["education"][0]["studyType"] = "PhD"
    bad["skills"][0]["keywords"] = ["Python", "Terraform"]
    scripted["resume"] = bad
    client.post(f"/resumes/{rid}/tailor")

    doc = client.get(f"/resumes/{rid}").json()

    assert len(doc["tailored"]["warnings"]) >= 3
    sent = finished(client, rid)
    assert sent.status_code == 200, sent.text
    for invented in ("73", "PhD", "Terraform", "Kubernetes"):
        assert invented not in sent.text


# ── Resumes tailored before this change ───────────────────────────────

LEGACY_NOTE = (
    "[work[0].highlights[2]] New term: 'Kubernetes' appears here but nowhere in your "
    "original resume. Keep it only if you have really worked with it and could answer "
    "an interviewer's follow-up. Otherwise edit the line to remove it.")


@pytest.fixture
def tailored_before(api):
    """A document as the earlier guard saved it: the term kept, and a
    note beside it that nothing required anyone to read."""
    client, rid, _ = api
    doc = server._load_resume_doc(rid)
    kept = with_line(2, "Consolidated build clusters on Kubernetes to reduce costs")
    doc.tailored = TailoredResume(
        resume=StructuredResume.model_validate(kept), changes=[], warnings=[LEGACY_NOTE])
    server._save_resume_doc(doc)
    return client, rid


def test_an_earlier_resume_with_an_unanswered_note_is_not_exported_as_finished(tailored_before):
    client, rid = tailored_before

    refused = finished(client, rid)

    assert refused.status_code == 409
    said = refused.json()["detail"]
    assert "Kubernetes" in said and "Initrode" in said
    assert "draft" in said


def test_it_can_still_be_downloaded_as_a_draft(tailored_before):
    client, rid = tailored_before

    draft = client.get(f"/resumes/{rid}/download?version=tailored&fmt=md&draft=true")

    assert draft.status_code == 200
    assert "DRAFT" in draft.headers["content-disposition"]


def test_taking_the_term_out_answers_the_note(tailored_before):
    client, rid = tailored_before

    client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [{
        "path": "work[0].highlights[2]", "value": LINES[2]}]})

    assert finished(client, rid).status_code == 200


def test_adding_it_to_the_resume_as_a_skill_answers_the_note(tailored_before):
    """The one confirmation that can be recorded today without a new
    field: the candidate puts it on their resume themselves."""
    client, rid = tailored_before

    added = client.post(f"/resumes/{rid}/add", json={
        "kind": "skill", "parent": "skills[0]", "text": "Kubernetes"})

    assert added.status_code == 200, added.text
    sent = finished(client, rid)
    assert sent.status_code == 200 and "on Kubernetes" in sent.text


def test_a_note_whose_line_has_moved_on_holds_nothing_up(tailored_before):
    client, rid = tailored_before
    doc = server._load_resume_doc(rid)
    doc.tailored.resume.work[0].highlights[2] = LINES[2]
    server._save_resume_doc(doc)

    assert finished(client, rid).status_code == 200


# ── Second follow-up review: F03a and F03b ────────────────────────────
# The note that blocks the export was the model's to keep or lose, and
# it was looked for only at the position it named. Either was enough to
# turn a refused export into a finished one with the claim still in it,
# and nobody had confirmed anything.

CLAIM = "Consolidated build clusters on Kubernetes to reduce costs"
FORMATS = ["md", "json", "docx", "pdf"]


def edit(client, rid, scripted, resume: dict, *, warnings=(), instruction="tidy it up"):
    scripted["resume"], scripted["warnings"] = resume, list(warnings)
    done = client.post(f"/resumes/{rid}/request-edit", json={"instruction": instruction})
    assert done.status_code == 200, done.text
    return done.json()


def legacy(index: int = 2, text: str = CLAIM) -> dict:
    return with_line(index, text)


def blocked_in_every_format(client, rid) -> None:
    for fmt in FORMATS:
        refused = finished(client, rid, fmt)
        assert refused.status_code == 409, (fmt, refused.status_code)
        assert "Kubernetes" in refused.json()["detail"], fmt


def drafts_still_offered(client, rid) -> None:
    for fmt in FORMATS:
        draft = client.get(f"/resumes/{rid}/download?version=tailored&fmt={fmt}&draft=true")
        assert draft.status_code == 200, fmt
        assert draft.headers["x-scrivio-export"] == "draft"
        assert "DRAFT" in draft.headers["content-disposition"]


@pytest.fixture
def held(tailored_before, api):
    """The legacy document, already refused once, and the means to edit it."""
    client, rid = tailored_before
    _, _, scripted = api
    assert finished(client, rid).status_code == 409
    return client, rid, scripted


# F03a: the model loses the note.

def test_f03a_the_review_case_an_unchanged_claim_and_no_warnings(held):
    client, rid, scripted = held

    edit(client, rid, scripted, legacy(), warnings=[],
         instruction="Put the strongest bullet first.")

    blocked_in_every_format(client, rid)
    drafts_still_offered(client, rid)


def test_f03a_a_reworded_claim_and_no_warnings(held):
    client, rid, scripted = held

    edit(client, rid, scripted,
         legacy(2, "Reduced costs by consolidating build clusters on Kubernetes"), warnings=[])

    blocked_in_every_format(client, rid)


def test_f03a_a_note_the_model_reworded_is_not_the_note(held):
    client, rid, scripted = held

    edit(client, rid, scripted, legacy(),
         warnings=["Kubernetes was reviewed and is fine to keep."])

    blocked_in_every_format(client, rid)


def test_f03a_a_note_the_model_wrote_for_itself_changes_nothing(held):
    """In the other direction: a model cannot raise a finding either.
    The list of what is unresolved is the application's."""
    client, rid, scripted = held
    clean = copy.deepcopy(ORIGINAL)

    after = edit(client, rid, scripted, clean, warnings=[
        "[work[0].highlights[0]] New term: 'Maintained' appears here but nowhere in "
        "your original resume."])

    assert finished(client, rid).status_code == 200
    assert not [w for w in after["tailored"]["warnings"] if "New term" in w]


def test_f03a_the_model_cannot_spread_a_flagged_name_to_another_line(held):
    """A name that was flagged is not a name the candidate gave. Being
    in the earlier version does not make it theirs."""
    client, rid, scripted = held
    spread = legacy()
    spread["work"][0]["highlights"][0] = "Maintained 12 services on Kubernetes behind the internal gateway"

    after = edit(client, rid, scripted, spread, warnings=[])

    lines = after["tailored"]["resume"]["work"][0]["highlights"]
    assert lines[0] == LINES[0]
    assert "Kubernetes" in lines[2]
    blocked_in_every_format(client, rid)


# F03b: the claim moves and the note does not.

def test_f03b_the_review_case_bullets_reordered_and_the_note_kept_word_for_word(held):
    client, rid, scripted = held
    moved = legacy()
    moved["work"][0]["highlights"] = [CLAIM, LINES[0], LINES[1]]

    after = edit(client, rid, scripted, moved, warnings=[LEGACY_NOTE])

    assert after["tailored"]["resume"]["work"][0]["highlights"][0] == CLAIM
    blocked_in_every_format(client, rid)
    drafts_still_offered(client, rid)


def test_f03b_an_earlier_bullet_is_removed_and_the_index_shifts(held):
    client, rid, scripted = held
    shorter = legacy()
    del shorter["work"][0]["highlights"][0]

    edit(client, rid, scripted, shorter, warnings=[LEGACY_NOTE])

    blocked_in_every_format(client, rid)


def test_f03b_the_candidate_removes_an_earlier_bullet_themselves(held):
    """The same shift, made by the candidate and not by a model."""
    client, rid, _ = held

    removed = client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [
        {"path": "work[0].highlights[0]", "value": ""}]})

    assert removed.status_code == 200, removed.text
    assert CLAIM in str(removed.json()["tailored"]["resume"])
    blocked_in_every_format(client, rid)


def test_f03b_the_note_is_corrected_to_where_the_claim_now_is(held):
    client, rid, scripted = held
    moved = legacy()
    moved["work"][0]["highlights"] = [CLAIM, LINES[0], LINES[1]]

    after = edit(client, rid, scripted, moved, warnings=[LEGACY_NOTE])

    notes = [w for w in after["tailored"]["warnings"] if "Kubernetes" in w]
    assert len(notes) == 1 and notes[0].startswith("[work[0].highlights[0]]")
    assert "the 1st bullet under Initrode" in finished(client, rid).json()["detail"]


TWO_JOBS = copy.deepcopy(ORIGINAL)
TWO_JOBS["work"].append({
    "name": "Hooli", "position": "Junior Developer",
    "startDate": "Jun 2018", "endDate": "Jan 2020", "summary": "",
    "highlights": ["Wrote inventory reports for the warehouse team"]})


@pytest.fixture
def two_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    scripted = {}
    monkeypatch.setattr(
        server, "_client_for_session",
        lambda *a, **k: (ScriptedClient(scripted["resume"], scripted.get("warnings", ())),
                         "balanced"))
    original = StructuredResume.model_validate(TWO_JOBS)
    kept = copy.deepcopy(TWO_JOBS)
    kept["work"][0]["highlights"][2] = CLAIM
    doc = ResumeDoc(
        resume_id="20260101-000000-two222", original_text=render_markdown(original),
        structured=original, jd_text=JD,
        report=run_ats_checks(render_markdown(original), JD, original),
        tailored=TailoredResume(resume=StructuredResume.model_validate(kept),
                                changes=[], warnings=[LEGACY_NOTE]))
    server._save_resume_doc(doc)
    client = TestClient(server.app)
    assert finished(client, doc.resume_id).status_code == 409
    return client, doc.resume_id, scripted, kept


@pytest.mark.parametrize("warnings", [[], [LEGACY_NOTE]], ids=["note-dropped", "note-kept"])
def test_f03b_a_work_entry_moves_and_the_old_entry_index_is_stale(two_jobs, warnings):
    client, rid, scripted, kept = two_jobs
    swapped = copy.deepcopy(kept)
    swapped["work"].reverse()

    after = edit(client, rid, scripted, swapped, warnings=warnings)

    jobs = after["tailored"]["resume"]["work"]
    assert [j["name"] for j in jobs] == ["Hooli", "Initrode"]
    assert CLAIM in jobs[1]["highlights"]
    blocked_in_every_format(client, rid)
    notes = [w for w in after["tailored"]["warnings"] if "Kubernetes" in w]
    assert len(notes) == 1 and notes[0].startswith("[work[1].highlights[2]]")


def test_f03b_a_claim_moved_to_another_job_is_not_kept_there(two_jobs):
    """Moving it under a different employer would make it a claim about
    a different job. It is not kept there, and with it gone from the
    resume there is nothing left to confirm."""
    client, rid, scripted, kept = two_jobs
    moved = copy.deepcopy(kept)
    moved["work"][0]["highlights"].remove(CLAIM)
    moved["work"][1]["highlights"].append(CLAIM)

    after = edit(client, rid, scripted, moved, warnings=[])

    assert "Kubernetes" not in str(after["tailored"]["resume"]["work"][1])
    text = str(after["tailored"]["resume"])
    assert (finished(client, rid).status_code == 409) == ("Kubernetes" in text)


# What does resolve it, and nothing else.

def test_taking_the_name_out_through_an_edit_permits_every_format(held):
    client, rid, scripted = held

    edit(client, rid, scripted, copy.deepcopy(ORIGINAL), warnings=[])

    for fmt in FORMATS:
        sent = finished(client, rid, fmt)
        assert sent.status_code == 200, fmt
        assert sent.headers["x-scrivio-export"] == "final"
    assert "Kubernetes" not in finished(client, rid).text


def test_the_candidate_taking_the_name_out_permits_every_format(held):
    client, rid, _ = held

    client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [
        {"path": "work[0].highlights[2]", "value": LINES[2]}]})

    for fmt in FORMATS:
        assert finished(client, rid, fmt).status_code == 200, fmt


def test_the_documented_confirmation_permits_every_format_after_the_claim_has_moved(held):
    """The candidate adds it to their resume themselves. That is the one
    confirmation there is a place to record, and it has to work wherever
    the claim has got to."""
    client, rid, scripted = held
    moved = legacy()
    moved["work"][0]["highlights"] = [CLAIM, LINES[0], LINES[1]]
    edit(client, rid, scripted, moved, warnings=[])
    blocked_in_every_format(client, rid)

    added = client.post(f"/resumes/{rid}/add", json={
        "kind": "skill", "parent": "skills[0]", "text": "Kubernetes"})

    assert added.status_code == 200, added.text
    for fmt in FORMATS:
        sent = finished(client, rid, fmt)
        assert sent.status_code == 200, fmt
    assert "on Kubernetes" in finished(client, rid).text


def test_naming_it_in_an_instruction_does_not_confirm_a_flagged_claim(held):
    """"Put the Kubernetes bullet first" names it and vouches for
    nothing. For a claim that is already flagged, an instruction is not
    the documented action, and a mention is not a confirmation."""
    client, rid, scripted = held
    moved = legacy()
    moved["work"][0]["highlights"] = [CLAIM, LINES[0], LINES[1]]

    edit(client, rid, scripted, moved, warnings=[],
         instruction="Put the Kubernetes bullet first.")

    blocked_in_every_format(client, rid)


# Saved, reloaded, undone.

def test_the_finding_is_in_the_saved_file_and_survives_a_reload(held):
    client, rid, scripted = held
    edit(client, rid, scripted, legacy(), warnings=[])

    on_disk = server._resume_path(rid).read_text(encoding="utf-8")
    again = server._load_resume_doc(rid)

    assert "New term: 'Kubernetes'" in on_disk
    assert [w for w in again.tailored.warnings if "Kubernetes" in w]
    assert server._export_refusal(again) is not None
    blocked_in_every_format(TestClient(server.app), rid)


def test_undoing_an_edit_that_lost_the_note_leaves_it_blocked(held):
    client, rid, scripted = held
    edit(client, rid, scripted, legacy(), warnings=[])

    undone = client.post(f"/resumes/{rid}/undo-tailored")

    assert undone.status_code == 200, undone.text
    blocked_in_every_format(client, rid)


def test_undoing_the_removal_brings_the_claim_and_the_block_back(held):
    client, rid, _ = held
    client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [
        {"path": "work[0].highlights[2]", "value": LINES[2]}]})
    assert finished(client, rid).status_code == 200

    undone = client.post(f"/resumes/{rid}/undo-tailored")

    assert CLAIM in str(undone.json()["tailored"]["resume"])
    blocked_in_every_format(client, rid)
    drafts_still_offered(client, rid)


def test_undoing_after_the_confirmation_does_not_undo_the_confirmation(held):
    """The confirmation is the candidate's addition to their own resume.
    Undo steps the tailored copy back. It does not take the skill off
    the resume, so the claim stays confirmed."""
    client, rid, _ = held
    client.post(f"/resumes/{rid}/add", json={
        "kind": "skill", "parent": "skills[0]", "text": "Kubernetes"})
    assert finished(client, rid).status_code == 200

    undone = client.post(f"/resumes/{rid}/undo-tailored")

    assert undone.status_code == 200, undone.text
    assert "Kubernetes" in str(server._load_resume_doc(rid).structured.skills)
    assert finished(client, rid).status_code == 200


def test_a_file_written_with_the_note_missing_is_put_right_when_next_saved(held):
    """Any code that saves the document, present or future, goes through
    one function. It is there that a finding is kept."""
    client, rid, _ = held
    doc = server._load_resume_doc(rid)
    doc.tailored.warnings = []

    server._save_resume_doc(doc)

    blocked_in_every_format(client, rid)


# At the guard, without the server.

def test_the_guard_carries_the_finding_whatever_the_model_returns():
    before = TailoredResume(
        resume=StructuredResume.model_validate(legacy()), changes=[], warnings=[LEGACY_NOTE])
    after = TailoredResume(
        resume=StructuredResume.model_validate(legacy()), changes=[], warnings=[])

    out = validate_model_output(
        StructuredResume.model_validate(ORIGINAL), after, baseline=before, jd_text=JD)

    assert [w for w in out.warnings if w.startswith("[work[0].highlights[2]] New term: 'Kubernetes'")]


def test_a_name_that_only_looks_the_same_does_not_answer_for_it():
    """"Go" is not confirmed by "Google" being on the resume."""
    from pipeline.workers.resume_fact_guard import open_findings

    original = copy.deepcopy(ORIGINAL)
    original["work"][0]["highlights"][0] = "Maintained 12 services behind the Google gateway"
    kept = copy.deepcopy(original)
    kept["work"][0]["highlights"][2] = "Consolidated build clusters written in Go"
    tailored = TailoredResume(
        resume=StructuredResume.model_validate(kept), changes=[],
        warnings=["[work[0].highlights[2]] New term: 'Go' appears here but nowhere in "
                  "your original resume."])

    found = open_findings(StructuredResume.model_validate(original), tailored)

    assert [(f.term, f.path) for f in found] == [("Go", "work[0].highlights[2]")]


# Other ways round, tried after the two in the review were closed.

def test_writing_the_name_another_way_does_not_answer_for_it(held):
    """"K8s" for "Kubernetes". The new spelling is itself a name nobody
    gave, so the line goes back to the one that was flagged."""
    client, rid, scripted = held

    after = edit(client, rid, scripted,
                 legacy(2, "Consolidated build clusters on K8s to reduce costs"), warnings=[])

    assert after["tailored"]["resume"]["work"][0]["highlights"][2] == CLAIM
    blocked_in_every_format(client, rid)


@pytest.mark.parametrize("written", ["kubernetes", "KUBERNETES", "Kubernetes-based tooling"])
def test_the_name_is_found_however_it_is_cased_or_joined(held, written):
    client, rid, scripted = held

    edit(client, rid, scripted,
         legacy(2, f"Consolidated build clusters on {written} to reduce costs"), warnings=[])

    assert finished(client, rid).status_code == 409


def test_the_name_moved_into_the_summary_is_not_kept_there(held):
    client, rid, scripted = held
    moved = copy.deepcopy(ORIGINAL)
    moved["basics"]["summary"] = "Software engineer who builds internal platforms on Kubernetes."

    after = edit(client, rid, scripted, moved, warnings=[])

    assert "Kubernetes" not in str(after["tailored"]["resume"])
    assert finished(client, rid).status_code == 200


def test_the_name_moved_into_the_skills_list_is_not_kept_there(held):
    client, rid, scripted = held
    moved = legacy()
    moved["skills"][0]["keywords"] = ["Python", "Kubernetes"]

    after = edit(client, rid, scripted, moved, warnings=[])

    assert after["tailored"]["resume"]["skills"][0]["keywords"] == ["Python"]
    blocked_in_every_format(client, rid)


def test_tailoring_again_from_the_original_leaves_nothing_to_answer_for(held):
    client, rid, scripted = held
    scripted["resume"], scripted["warnings"] = copy.deepcopy(ORIGINAL), []

    assert client.post(f"/resumes/{rid}/tailor").status_code == 200

    doc = client.get(f"/resumes/{rid}").json()
    assert "Kubernetes" not in str(doc["tailored"]["resume"])
    assert finished(client, rid).status_code == 200


def test_tailoring_again_cannot_bring_the_name_back(held):
    client, rid, scripted = held
    scripted["resume"], scripted["warnings"] = legacy(), []

    assert client.post(f"/resumes/{rid}/tailor").status_code == 200

    doc = client.get(f"/resumes/{rid}").json()
    assert "Kubernetes" not in str(doc["tailored"]["resume"])


def test_the_candidate_retyping_the_flagged_line_does_not_answer_for_it(held):
    """A limit, written down. The candidate's own typing is theirs on a
    resume with nothing flagged. On a flagged name there is no record of
    who typed what, so the one action that is recorded is the one that
    counts: adding it to the resume."""
    client, rid, _ = held

    client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [
        {"path": "work[0].highlights[2]",
         "value": "Consolidated our build clusters on Kubernetes, cutting costs"}]})

    blocked_in_every_format(client, rid)
    drafts_still_offered(client, rid)
