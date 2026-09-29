"""A name from the posting is recognised in a rewrite however it is cased
and wherever it falls.

The check for names a model has added went by capital letters and digits.
A model that wrote "kubernetes" in lower case got it past, the line was
kept, and the resume could be downloaded as finished with a claim nobody
had made. Recording that limit was the last thing the review of fe22bc1
left open.

The posting is where such names come from: a rewrite is an attempt to
sound like the posting. So the names the posting uses are collected, and
a rewrite is searched for them without regard to case.

What this does NOT do is said at the end of this file, as tests.
"""
import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import server
from pipeline.schemas.models import ResumeDoc, StructuredResume, TailoredResume
from pipeline.workers.resume_fact_guard import validate_model_output
from pipeline.workers.resume_studio_worker import render_markdown, run_ats_checks

FORMATS = ["md", "json", "docx", "pdf"]


def posting_names(text: str) -> set[str]:
    """Imported when called, so that on a commit from before the function
    existed these tests fail one by one and the rest still run."""
    from pipeline.workers import resume_fact_guard

    return resume_fact_guard.posting_names(text)

POSTING = """Senior Platform Engineer

About The Role
We are hiring an engineer to run our build and deploy platform. Design and
build reliable services. Collaborate with product teams. Own the roadmap.

Responsibilities:
- Lead incident response
- Mentor engineers
- Operate clusters on Kubernetes

Requirements
- Kubernetes in production
- Terraform
- Strong Python
- Experience with AWS and PostgreSQL

Nice To Have
- Familiarity with gRPC

Benefits: Competitive salary.
"""

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
    def __init__(self, resume, warnings=()):
        self.resume, self.warnings, self.messages = resume, list(warnings), self

    async def create(self, **kwargs):
        if kwargs.get("tools"):
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use",
                input={"resume": self.resume, "changes": [],
                       "warnings": list(self.warnings)})])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="")])


def with_line(index: int, text: str, base: dict = ORIGINAL) -> dict:
    changed = copy.deepcopy(base)
    changed["work"][0]["highlights"][index] = text
    return changed


def guarded(after: dict, *, before: dict | None = None, user_text: str = "",
            posting: str = POSTING, original: dict = ORIGINAL) -> TailoredResume:
    return validate_model_output(
        StructuredResume.model_validate(original),
        TailoredResume(resume=StructuredResume.model_validate(after), changes=[], warnings=[]),
        baseline=(TailoredResume(resume=StructuredResume.model_validate(before),
                                 changes=[], warnings=[]) if before else None),
        user_text=user_text, jd_text=posting)


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
        resume_id="20260101-000000-low111", original_text=render_markdown(original),
        structured=original, jd_text=POSTING,
        report=run_ats_checks(render_markdown(original), POSTING, original))
    server._save_resume_doc(doc)
    return TestClient(server.app), doc.resume_id, scripted


def tailor(client, rid, scripted, resume, warnings=()):
    scripted["resume"], scripted["warnings"] = resume, list(warnings)
    assert client.post(f"/resumes/{rid}/tailor").status_code == 200
    return client.get(f"/resumes/{rid}").json()


def edit(client, rid, scripted, resume, *, warnings=(), instruction="tidy it up"):
    scripted["resume"], scripted["warnings"] = resume, list(warnings)
    done = client.post(f"/resumes/{rid}/request-edit", json={"instruction": instruction})
    assert done.status_code == 200, done.text
    return done.json()


def finished(client, rid, fmt="md"):
    return client.get(f"/resumes/{rid}/download?version=tailored&fmt={fmt}")


def on_the_paper(doc) -> str:
    return str(doc["tailored"]["resume"]).casefold()


# ── The reproduction ──────────────────────────────────────────────────

def test_the_reproduction(api):
    """The original does not mention Kubernetes. The posting names it.
    The model writes it into a bullet in lower case."""
    client, rid, scripted = api

    doc = tailor(client, rid, scripted,
                 with_line(2, "Consolidated build clusters on kubernetes to reduce costs"))
    sent = finished(client, rid)

    assert sent.status_code == 200
    assert "kubernetes" not in sent.text.casefold()
    assert LINES[2] in sent.text
    assert any("kubernetes" in w.casefold() and "work[0].highlights[2]" in w
               for w in doc["tailored"]["warnings"])


# ── However it is cased, wherever it falls ────────────────────────────

WRITTEN = {
    "lower, mid-sentence": "Consolidated build clusters on kubernetes to reduce costs",
    "lower, first word": "kubernetes clusters consolidated to reduce costs",
    "lower, after a full stop": "Consolidated build clusters. kubernetes cut the costs",
    "lower, last word": "Consolidated build clusters to reduce costs with terraform",
    "lower, joined by a hyphen": "Consolidated kubernetes-based build clusters to reduce costs",
    "lower, in brackets": "Consolidated build clusters (kubernetes) to reduce costs",
    "lower, with a comma": "Consolidated build clusters on kubernetes, reducing costs",
    "capitalised, first word": "Kubernetes clusters consolidated to reduce costs",
    "capitalised, mid-sentence": "Consolidated build clusters on Kubernetes to reduce costs",
    "upper": "Consolidated build clusters on KUBERNETES to reduce costs",
    "mixed": "Consolidated build clusters on kUbErNeTeS to reduce costs",
    "an acronym in lower case": "Consolidated build clusters on aws to reduce costs",
    "a mixed-case name in lower case": "Consolidated build clusters, moving state to postgresql",
    "a name with an inner capital, lowered": "Consolidated build clusters behind grpc to reduce costs",
}


@pytest.mark.parametrize("line", WRITTEN.values(), ids=WRITTEN.keys())
def test_on_a_first_tailoring_the_line_is_put_back(line):
    out = guarded(with_line(2, line))

    assert out.resume.work[0].highlights == LINES
    assert any("work[0].highlights[2]" in w for w in out.warnings)


@pytest.mark.parametrize("line", WRITTEN.values(), ids=WRITTEN.keys())
def test_on_a_later_edit_the_line_is_put_back(line):
    earlier = with_line(2, "Consolidated build clusters, reducing costs")

    out = guarded(with_line(2, line, earlier), before=earlier)

    assert out.resume.work[0].highlights[2] == "Consolidated build clusters, reducing costs"


def test_the_headline_and_the_summary_are_held_to_it():
    changed = copy.deepcopy(ORIGINAL)
    changed["basics"]["label"] = "kubernetes platform engineer"
    changed["basics"]["summary"] = "Software engineer who builds internal platforms on kubernetes."

    out = guarded(changed)

    assert out.resume.basics.label == "Software Engineer"
    assert out.resume.basics.summary == ORIGINAL["basics"]["summary"]


def test_a_bullet_the_model_added_from_nothing_is_removed():
    changed = copy.deepcopy(ORIGINAL)
    changed["work"][0]["highlights"].append("ran terraform across three regions")

    out = guarded(changed)

    assert out.resume.work[0].highlights == LINES


def test_the_lines_around_it_keep_their_honest_rewording():
    changed = with_line(2, "Consolidated build clusters on kubernetes to reduce costs")
    changed["work"][0]["highlights"][1] = "Mentored engineers who joined the platform team"

    out = guarded(changed)

    assert out.resume.work[0].highlights[1] == "Mentored engineers who joined the platform team"
    assert out.resume.work[0].highlights[2] == LINES[2]


# ── Through the application, in every format ──────────────────────────

@pytest.mark.parametrize("fmt", FORMATS)
def test_a_first_tailoring_in_every_format(api, fmt):
    client, rid, scripted = api

    doc = tailor(client, rid, scripted, with_line(
        2, "Consolidated build clusters on kubernetes with terraform to reduce costs"))

    assert "kubernetes" not in on_the_paper(doc) and "terraform" not in on_the_paper(doc)
    sent = finished(client, rid, fmt)
    assert sent.status_code == 200, fmt
    assert sent.headers["x-scrivio-export"] == "final"
    if fmt in ("md", "json"):
        assert "kubernetes" not in sent.text.casefold()


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_later_edit_in_every_format(api, fmt):
    client, rid, scripted = api
    tailor(client, rid, scripted, copy.deepcopy(ORIGINAL))

    doc = edit(client, rid, scripted,
               with_line(2, "Consolidated build clusters on kubernetes to reduce costs"),
               instruction="make the last bullet match the posting")

    assert "kubernetes" not in on_the_paper(doc)
    sent = finished(client, rid, fmt)
    assert sent.status_code == 200, fmt
    if fmt in ("md", "json"):
        assert "kubernetes" not in sent.text.casefold()


def test_a_finding_the_model_writes_about_it_is_still_not_the_models_to_raise(api):
    """F03c holds. The line goes back because the application saw the
    name, and not because the model said so."""
    client, rid, scripted = api
    note = ("[work[0].highlights[2]] New term: 'kubernetes' appears here but nowhere in "
            "your original resume.")

    doc = tailor(client, rid, scripted, with_line(
        2, "Consolidated build clusters on kubernetes to reduce costs"), warnings=[note])

    assert "kubernetes" not in on_the_paper(doc)
    assert not [w for w in doc["tailored"]["warnings"] if "New term:" in w]
    assert finished(client, rid).status_code == 200


# ── Names that are the candidate's ────────────────────────────────────

def test_a_name_on_the_original_may_be_written_in_any_case():
    out = guarded(with_line(0, "Maintained 12 python services behind the internal gateway"))

    assert out.resume.work[0].highlights[0] == \
        "Maintained 12 python services behind the internal gateway"
    assert not out.warnings


def test_a_name_the_candidate_vouched_for_in_an_instruction_is_kept():
    out = guarded(with_line(2, "Consolidated build clusters on kubernetes to reduce costs"),
                  before=ORIGINAL,
                  user_text="Those clusters ran on kubernetes, I set them up")

    assert "on kubernetes" in out.resume.work[0].highlights[2]
    assert not [w for w in out.warnings if "kubernetes" in w.casefold()]


def test_a_name_the_candidate_refused_in_an_instruction_is_not():
    out = guarded(with_line(2, "Consolidated build clusters on kubernetes to reduce costs"),
                  before=ORIGINAL,
                  user_text="I have never used kubernetes, do not mention it")

    assert "kubernetes" not in render_markdown(out.resume).casefold()


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_name_the_candidate_typed_is_exported_and_survives_the_next_edit(api, fmt):
    client, rid, scripted = api
    tailor(client, rid, scripted, copy.deepcopy(ORIGINAL))
    typed = "Consolidated build clusters on kubernetes to reduce costs"
    assert client.post(f"/resumes/{rid}/edit-tailored", json={"edits": [
        {"path": "work[0].highlights[2]", "value": typed}]}).status_code == 200

    doc = edit(client, rid, scripted,
               with_line(2, "Reduced costs by consolidating build clusters on kubernetes"))

    assert "clusters on kubernetes" in str(doc["tailored"]["resume"])
    sent = finished(client, rid, fmt)
    assert sent.status_code == 200, fmt


def test_a_skill_the_candidate_added_may_be_used_in_lower_case(api):
    client, rid, scripted = api
    assert client.post(f"/resumes/{rid}/add", json={
        "kind": "skill", "parent": "skills[0]", "text": "Terraform"}).status_code == 200
    written = with_line(2, "Consolidated build clusters with terraform to reduce costs")
    written["skills"][0]["keywords"] = ["Python", "Terraform"]

    doc = tailor(client, rid, scripted, written)

    assert "with terraform" in str(doc["tailored"]["resume"])
    assert finished(client, rid).status_code == 200


# ── What must NOT be taken for a name ─────────────────────────────────

def test_what_the_posting_names():
    named = posting_names(POSTING)

    assert {"kubernetes", "terraform", "python", "aws", "postgresql", "grpc"} <= named


@pytest.mark.parametrize("word", [
    "senior", "platform", "engineer", "about", "the", "role", "we", "design", "collaborate",
    "own", "responsibilities", "lead", "mentor", "operate", "requirements", "strong",
    "experience", "nice", "to", "have", "familiarity", "benefits", "competitive",
    "production", "clusters", "salary", "incident", "response",
])
def test_a_heading_or_an_ordinary_word_in_the_posting_is_not_a_name(word):
    assert word not in posting_names(POSTING)


ORDINARY = [
    "Helped design and own the roadmap for the internal gateway",
    "Led incident response for 12 services behind the internal gateway",
    "Took the lead on requirements for the platform team",
    "Brought strong experience to the role, mentoring engineers",
    "Worked to collaborate with product teams and operate the build clusters",
    "design reviews, incident response, and mentoring for the platform team",
    "Familiarity with the benefits of consolidating build clusters to reduce costs",
    "Responsibilities grew to cover the production build clusters",
]


@pytest.mark.parametrize("line", ORDINARY)
def test_ordinary_prose_that_uses_the_postings_words_is_left_alone(line):
    """Every one of these words is in the posting, capitalised, at the
    start of a line or in a heading. None is on the original resume."""
    out = guarded(with_line(1, line))

    assert out.resume.work[0].highlights[1] == line
    assert not [w for w in out.warnings if "work[0].highlights[1]" in w]


@pytest.mark.parametrize("posting", [
    "REQUIREMENTS\n- AWS\n- SQL\n\nWHAT YOU WILL DO\n- BUILD SERVICES\n",
    "Requirements:\n- AWS\n- SQL\n\nWhat You Will Do:\n- Build services\n",
])
def test_headings_in_capitals_are_headings_and_acronyms_are_names(posting):
    named = posting_names(posting)

    assert {"aws", "sql"} <= named
    assert not named & {"requirements", "what", "you", "will", "do", "build", "services"}


def test_a_word_the_posting_also_uses_in_lower_case_is_a_word():
    posting = "Requirements:\n- Monitoring in production\n\nYou will own monitoring."

    assert "monitoring" not in posting_names(posting)


def test_a_posting_with_no_names_changes_nothing():
    out = guarded(with_line(2, "Consolidated build clusters, reducing our costs"),
                  posting="We need someone to look after our systems and help the team.")

    assert out.resume.work[0].highlights[2] == "Consolidated build clusters, reducing our costs"
    assert not out.warnings


@pytest.mark.parametrize("fmt", FORMATS)
def test_ordinary_prose_is_exported_as_written(api, fmt):
    client, rid, scripted = api

    doc = tailor(client, rid, scripted, with_line(1, ORDINARY[0]))

    assert ORDINARY[0] in str(doc["tailored"]["resume"])
    assert finished(client, rid, fmt).status_code == 200


# ── What this does not catch ──────────────────────────────────────────
# Each of these is a claim nobody made, reaching the paper. They are
# written as tests so that the list is a true one: a test here that
# starts failing means a gap has closed and the list is out of date.

def test_gap_a_posting_written_entirely_in_lower_case():
    """Nothing in the posting looks like a name, so nothing is collected
    from it."""
    posting = "requirements: kubernetes in production, terraform, strong python."

    assert posting_names(posting) == set()
    out = guarded(with_line(2, "Consolidated build clusters on kubernetes to reduce costs"),
                  posting=posting)

    assert "kubernetes" in out.resume.work[0].highlights[2]


def test_gap_a_technology_the_posting_does_not_mention():
    out = guarded(with_line(2, "Consolidated build clusters with ansible to reduce costs"))

    assert "ansible" in out.resume.work[0].highlights[2]


def test_gap_no_posting_at_all():
    out = guarded(with_line(2, "Consolidated build clusters on kubernetes to reduce costs"),
                  posting="")

    assert "kubernetes" in out.resume.work[0].highlights[2]


def test_gap_a_name_that_is_also_an_ordinary_word():
    """"Go", "React", "Spark", "Swift". In lower case there is no telling
    "react to incidents" from a claim to know React, so in lower case
    they are left alone. Capitalised in the middle of a sentence they
    are caught, as they always were."""
    posting = "Requirements:\n- Go\n- React\n- Spark\n"

    prose = guarded(with_line(1, "Helped the team go live and react to incidents"),
                    posting=posting)
    claim_in_lower_case = guarded(with_line(1, "Mentored engineers writing services in go"),
                                  posting=posting)
    claim_capitalised = guarded(with_line(1, "Mentored engineers writing services in Go"),
                                posting=posting)

    assert prose.resume.work[0].highlights[1] == "Helped the team go live and react to incidents"
    assert "in go" in claim_in_lower_case.resume.work[0].highlights[1]
    assert claim_capitalised.resume.work[0].highlights[1] == LINES[1]


def test_gap_a_word_made_from_a_name():
    out = guarded(with_line(2, "Consolidated build clusters and kubernetized the services"))

    assert "kubernetized" in out.resume.work[0].highlights[2]


def test_gap_a_name_spelled_another_way():
    """"k8s" is caught, because a digit makes it look like a name. "kube"
    is not: it is neither in the posting nor shaped like a name."""
    caught = guarded(with_line(2, "Consolidated build clusters on k8s to reduce costs"))
    missed = guarded(with_line(2, "Consolidated build clusters on kube to reduce costs"))

    assert caught.resume.work[0].highlights[2] == LINES[2]
    assert "kube" in missed.resume.work[0].highlights[2]


# ── A posting as they are written ─────────────────────────────────────

REALISTIC = """Staff Data Engineer (Remote, US)

Who We Are
Acme Health is a Series B company building tools for clinicians. Our Data Platform
team owns ingestion, modelling and reporting.

What You'll Do
- Architect and ship batch and streaming pipelines in Spark and Airflow
- Partner with Analytics and Product to define metrics
- Champion data quality; troubleshoot and debug production issues
- Spearhead our migration from Redshift to Snowflake
- Drive adoption of dbt across the org

What We're Looking For
- 8+ years in data engineering
- Expert SQL and Python; Scala a plus
- Hands-on with AWS (S3, Glue, EMR) and Terraform
- Excellent written and verbal communication
- Bonus: HIPAA, SOC 2, experience in Healthcare

Perks & Benefits
Unlimited PTO. 401(k) match. Medical, Dental, Vision.

Acme Health is an Equal Opportunity Employer.
"""


def test_a_realistic_posting_gives_up_its_technologies():
    assert {"spark", "airflow", "redshift", "snowflake", "sql", "python", "scala",
            "aws", "s3", "emr", "terraform", "hipaa"} <= posting_names(REALISTIC)


@pytest.mark.parametrize("word", [
    "staff", "data", "engineer", "remote", "who", "we", "are", "health", "company",
    "platform", "what", "do", "architect", "partner", "analytics", "product",
    "champion", "spearhead", "drive", "looking", "for", "expert", "hands",
    "excellent", "bonus", "healthcare", "perks", "benefits", "unlimited",
    "medical", "dental", "vision", "equal", "opportunity", "employer",
])
def test_a_realistic_posting_does_not_give_up_its_prose(word):
    assert word not in posting_names(REALISTIC)


@pytest.mark.parametrize("line", [
    "Helped architect and ship the internal gateway, partnering with product",
    "Took the lead on analytics for the platform team and drove adoption",
    "Spearheaded the consolidation of build clusters to reduce costs",
    "champion of data quality across 12 services behind the internal gateway",
    "Expert in consolidating build clusters, with excellent communication",
])
def test_prose_written_against_a_realistic_posting_is_left_alone(line):
    out = guarded(with_line(1, line), posting=REALISTIC)

    assert out.resume.work[0].highlights[1] == line
    assert not [w for w in out.warnings if "work[0].highlights[1]" in w]


@pytest.mark.parametrize("line", [
    "Consolidated build clusters, moving reporting to snowflake",
    "Consolidated build clusters and the airflow pipelines to reduce costs",
    "Consolidated build clusters on aws (s3, emr) to reduce costs",
    "Consolidated build clusters, keeping them hipaa compliant",
])
def test_names_from_a_realistic_posting_are_caught_in_lower_case(line):
    out = guarded(with_line(2, line), posting=REALISTIC)

    assert out.resume.work[0].highlights[2] == LINES[2]


def test_cost_an_ordinary_word_that_the_posting_uses_as_a_name():
    """The other kind of mistake: a line that was fine, put back. "Glue"
    is a product in this posting and a word everywhere else, and it is
    on neither list. The candidate is told which word it was, and can
    type the line themselves."""
    assert "glue" in posting_names(REALISTIC)

    out = guarded(with_line(2, "Consolidated build clusters, replacing the glue scripts"),
                  posting=REALISTIC)

    assert out.resume.work[0].highlights[2] == LINES[2]
    assert any("'glue'" in w for w in out.warnings)


def test_gap_a_name_the_posting_itself_writes_in_lower_case():
    """The posting says "dbt". Nothing about it looks like a name."""
    assert "dbt" not in posting_names(REALISTIC)

    out = guarded(with_line(2, "Consolidated build clusters and the dbt models"),
                  posting=REALISTIC)

    assert "dbt" in out.resume.work[0].highlights[2]
