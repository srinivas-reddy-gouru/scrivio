"""Factual guards on every model-written resume path (review items R01, R02).

Every fixture here is synthetic, and every model is a scripted stand-in:
the point is to drive the REAL worker and API paths with a response a
careless or adversarial model could produce, and see what reaches the
saved document. Testing the guard helper alone would prove the helper
works, not that anything calls it.
"""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import server
from pipeline.schemas.models import (
    ResumeDoc, StructuredResume, TailoredResume,
)
from pipeline.workers import resume_studio_worker as worker
from pipeline.workers.resume_fact_guard import validate_model_output
from pipeline.workers.resume_studio_worker import (
    METRIC_TOKEN, edit_resume_by_instruction, enforce_honesty,
    guard_edited_numbers_and_log, render_markdown, run_ats_checks,
    tailor_resume,
)

JD = (
    "Senior Platform Engineer.\n\nRequirements:\n- Kubernetes in production\n"
    "- Python services\n- Cost optimisation\n"
)

ORIGINAL = {
    "basics": {
        "name": "Sam Okafor", "label": "Software Engineer",
        "email": "sam@example.com", "phone": "+1 555 010 9999",
        "location": "Denver, CO",
        "summary": "Software engineer who builds internal platforms.",
    },
    "work": [{
        "name": "Initrode", "position": "Software Engineer",
        "startDate": "Feb 2020", "endDate": "Present", "summary": "",
        "highlights": [
            "Maintained 12 services behind the internal gateway",
            "Mentored engineers joining the platform team",
            "Consolidated build clusters to reduce costs",
        ],
    }],
    "education": [{
        "institution": "Colorado State University", "area": "Computer Science",
        "studyType": "BS", "startDate": "2014", "endDate": "2018", "score": "",
    }],
    "skills": [{"name": "Languages", "keywords": ["Python"]}],
    "projects": [], "certificates": [],
}


def _original() -> StructuredResume:
    return StructuredResume.model_validate(copy.deepcopy(ORIGINAL))


class ScriptedClient:
    """Answers tool calls with whatever resume it was scripted to return,
    and plain-text calls (the summary condenser) with `text`."""

    def __init__(self, resume: dict | None = None, *, text: str = "",
                 warnings: list[str] | None = None):
        self.resume, self.text = resume, text
        self.warnings = warnings or []
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools"):
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use",
                input={"resume": self.resume, "changes": [], "warnings": self.warnings},
            )])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


def _tailor(resume: dict, **client_kwargs) -> TailoredResume:
    original = _original()
    return asyncio.run(tailor_resume(
        structured=original, jd_text=JD, review=None,
        report=run_ats_checks(render_markdown(original), JD, original),
        client=ScriptedClient(resume, **client_kwargs),
    ))


def _all_text(t: TailoredResume) -> str:
    return render_markdown(t.resume)


# ── R01: the three reproduced inventions, through tailor_resume() ─────

def test_an_invented_metric_does_not_survive_initial_tailoring():
    invented = copy.deepcopy(ORIGINAL)
    invented["work"][0]["highlights"][2] = "Reduced costs by 73% by consolidating build clusters"

    out = _tailor(invented)

    assert "73" not in _all_text(out)
    assert METRIC_TOKEN in out.resume.work[0].highlights[2], \
        "the claim stays, the number becomes the user's to supply"
    assert any("73%" in w and "work[0].highlights[2]" in w for w in out.warnings), \
        "the warning must name the field and the figure it refused"


def test_a_degree_cannot_be_upgraded_at_the_same_institution():
    inflated = copy.deepcopy(ORIGINAL)
    inflated["education"][0]["studyType"] = "PhD"
    inflated["education"][0]["area"] = "Machine Learning"
    inflated["education"][0]["endDate"] = "2021"

    out = _tailor(inflated)

    edu = out.resume.education[0]
    assert (edu.studyType, edu.area) == ("BS", "Computer Science")
    assert (edu.startDate, edu.endDate) == ("2014", "2018")
    assert any("PhD" in w and "Colorado State University" in w for w in out.warnings)


def test_a_skill_with_no_basis_in_the_original_is_removed():
    padded = copy.deepcopy(ORIGINAL)
    padded["skills"][0]["keywords"] = ["Python", "Kubernetes"]

    out = _tailor(padded)

    assert [k for s in out.resume.skills for k in s.keywords] == ["Python"]
    assert any("Kubernetes" in w and "skill" in w.lower() for w in out.warnings)


def test_legitimate_rephrasing_keeps_its_own_numbers():
    """The guard must not be a machine for deleting true figures: a line
    rewritten around the number it already had keeps that number."""
    rephrased = copy.deepcopy(ORIGINAL)
    rephrased["work"][0]["highlights"][0] = (
        "Operated 12 services behind the internal gateway for the platform group")
    rephrased["basics"]["summary"] = (
        "Platform-focused software engineer with 6 years building internal tooling.")

    out = _tailor(rephrased)

    assert out.resume.work[0].highlights[0].startswith("Operated 12 services")
    assert "6 years" in out.resume.basics.summary, \
        "years of experience within the work dates are derivable, not invented"
    assert not [w for w in out.warnings if "Unsupported number" in w]


def test_a_summary_rewrite_cannot_reintroduce_what_the_guard_removed():
    """The condenser is a second model call, made after validation. If its
    output were trusted, it would be the way around every check above."""
    long_summary = copy.deepcopy(ORIGINAL)
    long_summary["basics"]["summary"] = " ".join(["Software engineer who builds internal platforms"] * 12)
    smuggled = ("Platform engineer with a PhD who cut costs by 73% "
                "running Kubernetes for internal platforms.")

    out = _tailor(long_summary, text=smuggled)

    summary = out.resume.basics.summary
    assert "73" not in summary and "PhD" not in summary and "Kubernetes" not in summary
    assert any("basics.summary" in w and "discarded" in w for w in out.warnings)


def test_a_safe_summary_condensation_is_still_applied():
    long_summary = copy.deepcopy(ORIGINAL)
    long_summary["basics"]["summary"] = " ".join(["Software engineer who builds internal platforms"] * 12)

    out = _tailor(long_summary, text="Software engineer who builds internal platforms.")

    assert out.resume.basics.summary == "Software engineer who builds internal platforms."


def test_a_new_named_term_in_prose_is_put_to_the_candidate_not_deleted():
    """Naming a technology the resume implies is tailoring; naming one it
    does not is fabrication. Code cannot tell which, so it asks."""
    stuffed = copy.deepcopy(ORIGINAL)
    stuffed["work"][0]["highlights"][2] = "Consolidated build clusters on Kubernetes to reduce costs"

    out = _tailor(stuffed)

    assert "Kubernetes" in out.resume.work[0].highlights[2]
    assert any("New term" in w and "Kubernetes" in w and "work[0].highlights[2]" in w
               for w in out.warnings)


def test_changed_contact_details_are_reverted():
    rewritten = copy.deepcopy(ORIGINAL)
    rewritten["basics"]["email"] = "sam.okafor@bigtech.example"
    rewritten["basics"]["name"] = "Dr. Sam Okafor"

    out = _tailor(rewritten)

    assert out.resume.basics.email == "sam@example.com"
    assert out.resume.basics.name == "Sam Okafor"
    assert any("basics.name" in w for w in out.warnings)


def test_a_dropped_job_is_restored():
    two_jobs = _original()
    two_jobs.work.append(two_jobs.work[0].model_copy(deep=True, update={
        "name": "Hooli", "position": "Junior Engineer",
        "startDate": "2018", "endDate": "2020",
        "highlights": ["Wrote deployment scripts"]}))
    dropped = TailoredResume(
        resume=two_jobs.model_copy(deep=True, update={"work": [two_jobs.work[0].model_copy(deep=True)]}),
        changes=[], warnings=[])

    guarded = enforce_honesty(two_jobs, dropped)

    assert [w.name for w in guarded.resume.work] == ["Initrode", "Hooli"]
    assert any("Restored" in w and "Hooli" in w for w in guarded.warnings)


# ── R02: numbers belong to claims, records keep their identity ────────

def _edit(before_resume: dict, after_resume: dict, user_text: str = "",
          original: dict | None = None) -> TailoredResume:
    before = TailoredResume(
        resume=StructuredResume.model_validate(before_resume), changes=[], warnings=[])
    after = TailoredResume(
        resume=StructuredResume.model_validate(after_resume), changes=[], warnings=[])
    return guard_edited_numbers_and_log(
        before, after, user_text,
        original=StructuredResume.model_validate(original or before_resume))


def test_a_number_cannot_migrate_from_one_claim_to_another():
    """The reproduced case. 12 is on the resume, in 'Maintained 12
    services', which says nothing about how many people were mentored."""
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 12 engineers joining the platform team"

    out = _edit(ORIGINAL, after)

    assert out.resume.work[0].highlights[1] == "Mentored engineers joining the platform team"
    assert any("work[0].highlights[1]" in w and "12" in w for w in out.warnings)


@pytest.mark.parametrize("source, rewritten, figure", [
    ("Cut hosting spend by $40,000 a year", "Grew revenue by $40,000 a year", "$40,000"),
    ("Reduced build failures 18% in a quarter", "Raised test coverage 18% in a quarter", "18%"),
    ("Brought p99 latency down to 240ms", "Brought startup time down to 240ms", "240ms"),
    ("Shipped the 2021 billing migration", "Won the 2021 engineering award", "2021"),
    ("Onboarded 30 engineers", "Interviewed 30 candidates", "30"),
])
def test_currencies_percentages_units_and_dates_stay_with_their_claim(source, rewritten, figure):
    before = copy.deepcopy(ORIGINAL)
    before["work"][0]["highlights"] = [source, "Wrote the team's runbooks"]
    after = copy.deepcopy(before)
    after["work"][0]["highlights"][1] = rewritten

    out = _edit(before, after)

    assert out.resume.work[0].highlights[1] == "Wrote the team's runbooks"
    assert any(figure in w for w in out.warnings)


def test_a_figure_does_not_move_between_employers():
    two = copy.deepcopy(ORIGINAL)
    two["work"].append({
        "name": "Hooli", "position": "Engineer", "startDate": "2018", "endDate": "2020",
        "summary": "", "highlights": ["Supported the on-call rotation"]})
    after = copy.deepcopy(two)
    after["work"][1]["highlights"][0] = "Maintained 12 services on the on-call rotation"

    out = _edit(two, after)

    assert out.resume.work[1].highlights[0] == "Supported the on-call rotation"


def test_a_number_the_user_supplied_is_applied():
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 9 engineers joining the platform team"

    out = _edit(ORIGINAL, after, user_text="I mentored 9 engineers over that time")

    assert "Mentored 9 engineers" in out.resume.work[0].highlights[1]
    assert not out.warnings


def test_a_number_the_coach_suggested_is_not_the_users_number():
    """The chat history holds model turns too. Counting them as the user's
    words would let a figure the model made up approve itself."""
    from pipeline.workers.resume_studio_worker import users_own_words

    words = users_own_words("apply what you recommended", [
        {"role": "assistant", "content": "Say you mentored 25 engineers."},
        {"role": "user", "content": "ok, sounds good"},
    ])
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 25 engineers joining the platform team"

    out = _edit(ORIGINAL, after, user_text=words)

    assert "25" not in out.resume.work[0].highlights[1]


PROMOTED = {
    "basics": {"name": "Ana Reyes"},
    "work": [
        {"name": "Vandelay", "position": "Senior Engineer", "startDate": "Jan 2022",
         "endDate": "Present", "summary": "", "highlights": ["Led the storage team"]},
        {"name": "Vandelay", "position": "Engineer", "startDate": "Mar 2019",
         "endDate": "Dec 2021", "summary": "", "highlights": ["Built the ingest service"]},
    ],
    "education": [
        {"institution": "Tech University", "studyType": "MS", "area": "Computer Science",
         "startDate": "2017", "endDate": "2019"},
        {"institution": "Tech University", "studyType": "BS", "area": "Mathematics",
         "startDate": "2013", "endDate": "2017"},
    ],
}


def test_two_roles_at_one_employer_keep_their_own_titles_and_dates():
    original = StructuredResume.model_validate(PROMOTED)
    reordered = copy.deepcopy(PROMOTED)
    reordered["work"].reverse()                    # tailoring may reorder
    reordered["work"][0]["highlights"] = ["Built the ingestion service"]
    tailored = TailoredResume(
        resume=StructuredResume.model_validate(reordered), changes=[], warnings=[])

    guarded = enforce_honesty(original, tailored)

    assert [(w.position, w.startDate, w.endDate) for w in guarded.resume.work] == [
        ("Engineer", "Mar 2019", "Dec 2021"),
        ("Senior Engineer", "Jan 2022", "Present"),
    ]
    assert guarded.warnings == [], "nothing was altered, so nothing is reported"


def test_a_promotion_cannot_be_backdated_onto_the_earlier_role():
    original = StructuredResume.model_validate(PROMOTED)
    inflated = copy.deepcopy(PROMOTED)
    inflated["work"][1]["position"] = "Senior Engineer"      # the 2019 role, retitled
    tailored = TailoredResume(
        resume=StructuredResume.model_validate(inflated), changes=[], warnings=[])

    guarded = enforce_honesty(original, tailored)

    assert [w.position for w in guarded.resume.work] == ["Senior Engineer", "Engineer"]
    assert any("Reverted job title" in w for w in guarded.warnings)


def test_two_degrees_from_one_school_stay_distinct():
    original = StructuredResume.model_validate(PROMOTED)
    merged = copy.deepcopy(PROMOTED)
    merged["education"][1]["studyType"] = "MS"               # the BS, upgraded
    merged["education"][1]["area"] = "Computer Science"
    tailored = TailoredResume(
        resume=StructuredResume.model_validate(merged), changes=[], warnings=[])

    guarded = enforce_honesty(original, tailored)

    assert [(e.studyType, e.area) for e in guarded.resume.education] == [
        ("MS", "Computer Science"), ("BS", "Mathematics")]


def test_a_bullet_added_to_the_second_role_mirrors_into_the_second_role():
    """Matching by employer name alone would file it under the first."""
    from pipeline.workers.resume_studio_worker import mirror_append

    original = StructuredResume.model_validate(PROMOTED)
    shown = StructuredResume.model_validate(PROMOTED)
    shown.work[1].highlights.append("Ran the 2020 migration")

    assert mirror_append(shown, original, "work[1].highlights[+]", "Ran the 2020 migration")

    assert original.work[1].highlights == ["Built the ingest service", "Ran the 2020 migration"]
    assert original.work[0].highlights == ["Led the storage team"]


# ── The extracted structure is itself model output ───────────────────

def test_the_extraction_audit_flags_what_the_uploaded_text_does_not_contain():
    from pipeline.workers.resume_fact_guard import audit_extraction

    uploaded = (
        "Sam Okafor\nSoftware Engineer, Initrode\nFeb 2020 - Present\n"
        "- Maintained 12 services behind the internal gateway\n"
        "Colorado State University, BS Computer Science 2014-2018\nSkills: Python\n")
    extracted = _original()
    extracted.skills[0].keywords.append("Kubernetes")
    extracted.work[0].highlights[1] = "Mentored 40 engineers"

    findings = audit_extraction(uploaded, extracted)

    assert findings == ["the skill 'Kubernetes'", "the figure 40 under Initrode"], \
        "exactly the two inventions, and nothing the upload does contain"


def test_the_audit_survives_pdf_spacing_damage():
    """PDF text extraction splits words ('T echnical'). Flagging every such
    word as an invention would bury the real findings."""
    from pipeline.workers.resume_fact_guard import audit_extraction

    extracted = StructuredResume.model_validate({
        "basics": {"name": "A"},
        "work": [{"name": "Initrode", "position": "Technical Lead", "highlights": []}]})

    assert audit_extraction("A\nT echnical Lead, Init rode\n", extracted) == []


# ── The API paths, with a scripted model ─────────────────────────────

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    scripted = {}
    monkeypatch.setattr(
        server, "_client_for_session",
        lambda *a, **k: (ScriptedClient(scripted["resume"]), "balanced"))
    original = _original()
    doc = ResumeDoc(
        resume_id="20260101-000000-abc123", original_text=render_markdown(original),
        structured=original, jd_text=JD,
        report=run_ats_checks(render_markdown(original), JD, original))
    server._save_resume_doc(doc)
    return TestClient(server.app), doc.resume_id, scripted


def test_the_tailor_endpoint_persists_only_the_guarded_resume(api):
    client, rid, scripted = api
    bad = copy.deepcopy(ORIGINAL)
    bad["work"][0]["highlights"][2] = "Reduced costs by 73% by consolidating build clusters"
    bad["education"][0]["studyType"] = "PhD"
    bad["skills"][0]["keywords"] = ["Python", "Kubernetes"]
    scripted["resume"] = bad

    assert client.post(f"/resumes/{rid}/tailor").status_code == 200
    saved = client.get(f"/resumes/{rid}").json()["tailored"]

    text = str(saved["resume"])
    assert "73" not in text and "PhD" not in text
    assert saved["resume"]["skills"][0]["keywords"] == ["Python"]
    assert len([w for w in saved["warnings"] if "73%" in w or "PhD" in w or "Kubernetes" in w]) == 3


def test_the_edit_endpoint_refuses_a_migrated_number(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    moved = copy.deepcopy(ORIGINAL)
    moved["work"][0]["highlights"][1] = "Mentored 12 engineers joining the platform team"
    moved["education"][0]["studyType"] = "PhD"
    scripted["resume"] = moved

    r = client.post(f"/resumes/{rid}/request-edit",
                    json={"instruction": "make the mentoring bullet stronger"})

    assert r.status_code == 200, r.text
    saved = r.json()["tailored"]
    assert saved["resume"]["work"][0]["highlights"][1] == "Mentored engineers joining the platform team"
    assert saved["resume"]["education"][0]["studyType"] == "BS"


def test_what_the_user_added_survives_the_guard_on_the_next_edit(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    added = client.post(f"/resumes/{rid}/add", json={
        "kind": "bullet", "parent": "work[0]",
        "text": "Cut build time by 35% across 40 repositories"}).json()
    scripted["resume"] = added["tailored"]["resume"]        # the model carries it through

    r = client.post(f"/resumes/{rid}/request-edit", json={"instruction": "tidy the wording"})

    highlights = r.json()["tailored"]["resume"]["work"][0]["highlights"]
    assert "Cut build time by 35% across 40 repositories" in highlights


# ── F02: a figure that stays on its line and changes what it counts ───
# The follow-up review's reproduction, exactly. The line had a 12 in it,
# so any 12 on that line was accepted without asking what it measured.

def test_a_figure_kept_on_its_line_cannot_change_what_it_counts():
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][0] = "Mentored 12 engineers"

    out = _edit(ORIGINAL, after)

    assert out.resume.work[0].highlights[0] == "Maintained 12 services behind the internal gateway"
    assert any("12" in w for w in out.warnings)


def test_the_exact_case_from_the_follow_up_review():
    original = StructuredResume.model_validate({
        "basics": {"name": "Sam Okafor"},
        "work": [{"name": "Initrode", "position": "Software Engineer",
                  "startDate": "Feb 2020", "endDate": "Present",
                  "highlights": ["Maintained 12 services."]}]})
    baseline = TailoredResume(resume=original.model_copy(deep=True), changes=[], warnings=[])
    replaced = original.model_copy(deep=True)
    replaced.work[0].highlights[0] = "Mentored 12 engineers."

    out = validate_model_output(
        original, TailoredResume(resume=replaced, changes=[], warnings=[]),
        baseline=baseline)

    assert out.resume.work[0].highlights[0] == "Maintained 12 services."
    assert out.warnings


MEASURED = {
    "basics": {"name": "Sam Okafor"},
    "work": [{"name": "Initrode", "position": "Software Engineer",
              "startDate": "Feb 2020", "endDate": "Present", "summary": "",
              "highlights": [
                  "Cut build failures by 40% after moving to hermetic builds",
                  "Saved $30,000 a year in cloud storage",
                  "Reduced deploy time from 45 minutes to 6 minutes",
              ]}],
}


@pytest.mark.parametrize("line, replacement", [
    (0, "Grew revenue by 40% after moving to hermetic builds"),
    (0, "Raised test coverage 40%"),
    (1, "Won $30,000 in new contracts"),
    (1, "Managed a $30,000 training budget"),
    (2, "Reduced customer churn from 45 accounts to 6 accounts"),
])
def test_a_percentage_or_amount_cannot_be_moved_to_another_claim_in_place(line, replacement):
    after = copy.deepcopy(MEASURED)
    after["work"][0]["highlights"][line] = replacement

    out = _edit(MEASURED, after)

    assert out.resume.work[0].highlights[line] == MEASURED["work"][0]["highlights"][line]
    assert out.warnings


@pytest.mark.parametrize("line, rewrite", [
    (0, "Reduced build failures 40% by moving to hermetic builds"),
    (0, "Moved to hermetic builds, cutting failures in the build by 40 percent"),
    (1, "Cut cloud storage spend by $30,000 a year"),
    (1, "Saved $30,000 annually on cloud storage"),
    (2, "Brought deploy time down from 45 minutes to 6 minutes"),
    (2, "Cut deploys from 45 minutes to 6 minutes"),
])
def test_rewording_the_same_claim_in_place_keeps_its_figure(line, rewrite):
    """The other half. A guard that refuses every reworded figure would
    make the editor useless, and people would stop reading its warnings."""
    after = copy.deepcopy(MEASURED)
    after["work"][0]["highlights"][line] = rewrite

    out = _edit(MEASURED, after)

    assert out.resume.work[0].highlights[line] == rewrite
    assert not out.warnings


def test_a_figure_the_user_filled_in_survives_a_later_rewording():
    """It is in the earlier version of the line and nowhere in the
    original. The earlier version has to go on counting as a source."""
    filled = copy.deepcopy(ORIGINAL)
    filled["work"][0]["highlights"][1] = "Mentored 9 engineers joining the platform team"
    reworded = copy.deepcopy(filled)
    reworded["work"][0]["highlights"][1] = "Coached 9 engineers who joined the platform team"

    out = _edit(filled, reworded, original=ORIGINAL)

    assert out.resume.work[0].highlights[1] == "Coached 9 engineers who joined the platform team"


def test_a_figure_the_user_filled_in_cannot_then_be_moved():
    filled = copy.deepcopy(ORIGINAL)
    filled["work"][0]["highlights"][1] = "Mentored 9 engineers joining the platform team"
    moved = copy.deepcopy(filled)
    moved["work"][0]["highlights"][1] = "Shipped 9 products with the platform team"

    out = _edit(filled, moved, original=ORIGINAL)

    assert out.resume.work[0].highlights[1] == "Mentored 9 engineers joining the platform team"


# A number in what the user typed is not the user vouching for it.

@pytest.mark.parametrize("said", [
    "Do not say 25 engineers, I never managed anyone",
    "Remove the claim about 25 engineers",
    "I did not mentor 25 engineers, that is wrong",
    "It was never 25 engineers",
    "The coach suggested 25 engineers but that is not true",
])
def test_a_number_the_user_rejected_is_not_a_number_the_user_supplied(said):
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 25 engineers joining the platform team"

    out = _edit(ORIGINAL, after, user_text=said)

    assert "25" not in out.resume.work[0].highlights[1]


def test_a_number_the_user_gave_for_one_thing_is_not_licence_for_another():
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 9 engineers joining the platform team"
    after["work"][0]["highlights"][2] = "Consolidated 9 build clusters to reduce costs"

    out = _edit(ORIGINAL, after, user_text="I mentored 9 engineers over that time")

    assert "Mentored 9 engineers" in out.resume.work[0].highlights[1]
    assert out.resume.work[0].highlights[2] == "Consolidated build clusters to reduce costs"


@pytest.mark.parametrize("said", [
    "make it 9", "the number is 9", "9", "it was 9 engineers", "use 9 there",
])
def test_a_bare_number_from_the_user_is_still_the_users_number(said):
    """Someone answering "how many?" types a number and nothing else."""
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 9 engineers joining the platform team"

    out = _edit(ORIGINAL, after, user_text=said)

    assert "Mentored 9 engineers" in out.resume.work[0].highlights[1]


def test_the_edit_endpoint_refuses_a_figure_moved_in_place(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    moved = copy.deepcopy(ORIGINAL)
    moved["work"][0]["highlights"][0] = "Mentored 12 engineers"
    scripted["resume"] = moved

    r = client.post(f"/resumes/{rid}/request-edit",
                    json={"instruction": "make the first bullet about people"})

    assert r.status_code == 200, r.text
    saved = r.json()["tailored"]
    assert saved["resume"]["work"][0]["highlights"][0] == \
        "Maintained 12 services behind the internal gateway"
    assert any("12" in w for w in saved["warnings"])


def test_the_edit_endpoint_does_not_take_a_rejected_number_as_supplied(api):
    client, rid, scripted = api
    scripted["resume"] = copy.deepcopy(ORIGINAL)
    client.post(f"/resumes/{rid}/tailor")
    wrong = copy.deepcopy(ORIGINAL)
    wrong["work"][0]["highlights"][1] = "Mentored 25 engineers joining the platform team"
    scripted["resume"] = wrong

    r = client.post(f"/resumes/{rid}/request-edit", json={
        "instruction": "Do not say 25 engineers anywhere, I never managed anyone"})

    assert r.status_code == 200, r.text
    assert "25" not in r.json()["tailored"]["resume"]["work"][0]["highlights"][1]


@pytest.mark.parametrize("said", [
    "The coach suggested 25 engineers",
    "you recommended 25 engineers earlier",
    "It said 25 engineers",
])
def test_repeating_what_was_proposed_is_not_agreeing_to_it(said):
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 25 engineers joining the platform team"

    out = _edit(ORIGINAL, after, user_text=said)

    assert "25" not in out.resume.work[0].highlights[1]


@pytest.mark.parametrize("said", [
    "You suggested 9 engineers, that is right, use it",
    "apply what you suggested: 9 engineers",
    "yes, the coach said 9 engineers and I agree",
])
def test_agreeing_to_what_was_proposed_makes_it_the_users(said):
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 9 engineers joining the platform team"

    out = _edit(ORIGINAL, after, user_text=said)

    assert "Mentored 9 engineers" in out.resume.work[0].highlights[1]


def test_a_bare_number_is_good_for_one_place_only():
    """"Make it 9" does not say where. It is taken to mean one place."""
    after = copy.deepcopy(ORIGINAL)
    after["work"][0]["highlights"][1] = "Mentored 9 engineers joining the platform team"
    after["work"][0]["highlights"][2] = "Consolidated 9 build clusters to reduce costs"

    out = _edit(ORIGINAL, after, user_text="make it 9")

    kept = [h for h in out.resume.work[0].highlights if "9" in h]
    assert len(kept) == 1
