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
    def __init__(self, resume):
        self.resume, self.messages = resume, self

    async def create(self, **kwargs):
        if kwargs.get("tools"):
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use",
                input={"resume": self.resume, "changes": [], "warnings": []})])
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
        lambda *a, **k: (ScriptedClient(scripted["resume"]), "balanced"))
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
