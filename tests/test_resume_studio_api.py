"""Resume studio API: create (paste/file/JSON Resume/saved job target),
tailor with the honesty flow, downloads in three formats, CRUD."""
import base64
import io
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import server
from main import MockAnthropicClient


RESUME_TEXT = """Jordan Rivera
Backend Engineer
jordan@example.com · +1 555 010 1234 · Austin, TX

Summary
Backend engineer focused on event-driven systems.

Experience

Software Engineer — Acme Corp
Jan 2021 – Present
- Built Kafka pipelines processing 2M events/day
- Cut p99 latency 40% by rewriting the consumer group logic
- Mentored two junior engineers

Education
B.S. in Computer Science — State University
2015 – 2019

Skills
- Languages: Python, Go
- Infrastructure: Kafka, PostgreSQL
"""

JD = (
    "Senior Backend Engineer at Acme.\n\nRequirements:\n"
    "- 5+ years Python\n- Kubernetes in production\n- Kafka event streaming\n"
    "- PostgreSQL\n"
)


@pytest.fixture(autouse=True)
def _isolate_output_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(server, "OUTPUT_ROOT", Path(td))
        yield


@pytest.fixture(autouse=True)
def _mock_anthropic_for_api(monkeypatch):
    monkeypatch.setattr(
        server, "_anthropic_client", lambda request: MockAnthropicClient(request)
    )


def _create(client: TestClient, **overrides) -> dict:
    body = {"resume_text": RESUME_TEXT}
    body.update(overrides)
    response = client.post("/resumes", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ── Create ───────────────────────────────────────────────────────────

def test_create_without_jd_gives_report_and_review():
    client = TestClient(server.app)
    doc = _create(client)
    assert doc["report"]["score"] > 0
    assert doc["report"]["keyword_coverage"] is None
    check_ids = {c["id"] for c in doc["report"]["checks"]}
    assert {"contact-info", "quantification", "work-dates", "skills-section"} <= check_ids
    assert doc["review"]["missing_keywords"] == []  # no JD → no keyword claims
    assert doc["structured"]["basics"]["name"]  # extraction ran
    assert doc["jd_label"] == ""


def test_create_with_pasted_jd_adds_coverage():
    client = TestClient(server.app)
    doc = _create(client, jd_text=JD)
    coverage = doc["report"]["keyword_coverage"]
    assert coverage is not None and coverage["percent"] > 0
    assert "kubernetes" in coverage["missing"]
    assert doc["review"]["missing_keywords"] == ["Kubernetes"]
    assert doc["jd_label"] == "pasted JD"


def test_create_with_saved_job_target():
    client = TestClient(server.app)
    profile = client.post("/job-profiles", json={
        "role_title": "Senior Backend Engineer", "company": "Stripe",
        "job_description": JD, "resume_text": RESUME_TEXT,
    })
    assert profile.status_code == 200, profile.text
    profile_id = profile.json()["profile"]["profile_id"]
    doc = _create(client, job_profile_id=profile_id)
    assert doc["jd_label"] == "Senior Backend Engineer @ Stripe"
    assert doc["report"]["keyword_coverage"] is not None


def test_create_via_txt_upload():
    client = TestClient(server.app)
    b64 = base64.b64encode(RESUME_TEXT.encode()).decode()
    doc = _create(
        client, resume_text="", resume_file_b64=b64, resume_filename="resume.txt"
    )
    assert doc["original_text"].startswith("Jordan Rivera")


def test_create_via_jsonresume_upload_skips_extraction(monkeypatch):
    async def boom(*args, **kwargs):  # extraction must NOT be called
        raise AssertionError("extract_resume called for a JSON Resume upload")

    monkeypatch.setattr(server, "extract_resume", boom)
    client = TestClient(server.app)
    jsonresume = {
        "basics": {"name": "Jordan Rivera", "email": "jordan@example.com",
                   "phone": "555-0100"},
        "work": [{"name": "Acme Corp", "position": "Software Engineer",
                  "startDate": "Jan 2021", "endDate": "Present",
                  "highlights": ["Built Kafka pipelines processing 2M events/day"]}],
        "skills": [{"name": "Languages", "keywords": ["Python"]}],
    }
    b64 = base64.b64encode(json.dumps(jsonresume).encode()).decode()
    doc = _create(
        client, resume_text="", resume_file_b64=b64, resume_filename="resume.json"
    )
    assert doc["structured"]["work"][0]["name"] == "Acme Corp"
    assert "# Jordan Rivera" in doc["original_text"]  # rendered from structure


def test_create_validation_errors():
    client = TestClient(server.app)
    assert client.post("/resumes", json={}).status_code == 422
    bad_b64 = client.post("/resumes", json={
        "resume_file_b64": "!!!not-base64!!!", "resume_filename": "r.txt",
    })
    assert bad_b64.status_code == 422
    bad_json = client.post("/resumes", json={
        "resume_file_b64": base64.b64encode(b"{ not json").decode(),
        "resume_filename": "r.json",
    })
    assert bad_json.status_code == 422


# ── Tailor ───────────────────────────────────────────────────────────

def test_tailor_requires_jd():
    client = TestClient(server.app)
    doc = _create(client)  # no JD
    response = client.post(f"/resumes/{doc['resume_id']}/tailor")
    assert response.status_code == 422
    assert "job description" in response.json()["detail"]


def test_tailor_produces_before_after_and_change_log():
    client = TestClient(server.app)
    doc = _create(client, jd_text=JD)
    response = client.post(f"/resumes/{doc['resume_id']}/tailor")
    assert response.status_code == 200, response.text
    out = response.json()
    tailored = out["tailored"]
    assert tailored["changes"], "change log must not be empty"
    kinds = {c["kind"] for c in tailored["changes"]}
    assert "placeholder" in kinds
    assert any("[METRIC]" in w for w in tailored["warnings"])
    assert any("Cannot honestly claim" in w for w in tailored["warnings"])
    # Honesty guard held: same employer, title, dates as the original.
    work = tailored["resume"]["work"][0]
    assert (work["name"], work["position"]) == ("Acme Corp", "Software Engineer")
    assert (work["startDate"], work["endDate"]) == ("Jan 2021", "Present")
    # Before/after reports both present and both scored.
    assert out["report"]["score"] > 0
    assert out["tailored_report"]["score"] > 0
    assert out["tailored_report"]["keyword_coverage"] is not None


# ── Downloads ────────────────────────────────────────────────────────

def test_download_markdown_and_json():
    client = TestClient(server.app)
    doc = _create(client)
    rid = doc["resume_id"]
    md = client.get(f"/resumes/{rid}/download?fmt=md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "attachment" in md.headers["content-disposition"]
    assert "## Experience" in md.text
    js = client.get(f"/resumes/{rid}/download?fmt=json")
    assert js.status_code == 200
    exported = js.json()
    assert exported["basics"]["name"] == "Jordan Rivera"
    assert "$schema" in exported


def test_download_docx_round_reads():
    from docx import Document

    client = TestClient(server.app)
    doc = _create(client)
    response = client.get(f"/resumes/{doc['resume_id']}/download?fmt=docx")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    parsed = Document(io.BytesIO(response.content))
    assert any("Jordan Rivera" in p.text for p in parsed.paragraphs)


def test_download_pdf():
    client = TestClient(server.app)
    doc = _create(client)
    response = client.get(f"/resumes/{doc['resume_id']}/download?fmt=pdf")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 1000


def test_download_tailored_version_gating():
    client = TestClient(server.app)
    doc = _create(client, jd_text=JD)
    rid = doc["resume_id"]
    assert client.get(f"/resumes/{rid}/download?version=tailored").status_code == 404
    assert client.post(f"/resumes/{rid}/tailor").status_code == 200
    tailored_md = client.get(f"/resumes/{rid}/download?version=tailored")
    assert tailored_md.status_code == 200
    assert "[METRIC]" in tailored_md.text
    assert client.get(f"/resumes/{rid}/download?fmt=rtf").status_code == 422
    assert client.get(f"/resumes/{rid}/download?version=draft").status_code == 422


# ── CRUD ─────────────────────────────────────────────────────────────

def test_list_get_delete_roundtrip():
    client = TestClient(server.app)
    doc = _create(client, jd_text=JD)
    rid = doc["resume_id"]
    listing = client.get("/resumes").json()
    assert [item["resume_id"] for item in listing] == [rid]
    assert listing[0]["name"] == "Jordan Rivera"
    assert listing[0]["jd_label"] == "pasted JD"
    assert listing[0]["score"] == doc["report"]["score"]
    fetched = client.get(f"/resumes/{rid}")
    assert fetched.status_code == 200
    assert fetched.json()["resume_id"] == rid
    assert client.delete(f"/resumes/{rid}").json() == {"deleted": rid}
    assert client.get(f"/resumes/{rid}").status_code == 404
    assert client.get("/resumes").json() == []


def test_path_traversal_ids_rejected():
    client = TestClient(server.app)
    for bad in ("invalid~id", "x" * 100, "20250101..000000-abcdef"):
        assert client.get(f"/resumes/{bad}").status_code == 404
        assert client.delete(f"/resumes/{bad}").status_code == 404
