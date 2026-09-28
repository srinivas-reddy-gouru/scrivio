"""Data controls (review item R20): what is held, taking it out, putting
it back, and removing it.

Every record here is synthetic and lives in a temporary directory.
"""
import io
import json
import logging
import zipfile

import pytest
from fastapi.testclient import TestClient

from api import data_controls, server
from api import data as data_cli
from pipeline.schemas.models import ResumeDoc, StructuredResume

SECRET_LINE = "Led the Halberd migration for 40 engineers at Initrode"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """One of everything."""
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setenv("ARTICLE_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("SCRIVIO_DEMO", raising=False)
    client = TestClient(server.app)

    server._save_resume_doc(ResumeDoc(
        resume_id="20260101-000000-aaa111", original_text=f"Sam Okafor\n{SECRET_LINE}",
        structured=StructuredResume.model_validate({"basics": {"name": "Sam Okafor"}})))
    profile = client.post("/job-profiles", json={
        "role_title": "Backend Engineer", "company": "Initrode",
        "job_description": "Build services. " * 30, "resume_text": f"Sam Okafor. {SECRET_LINE}. " * 8,
    }).json()["profile"]["profile_id"]
    session = client.post("/interviews", json={
        "mode": "job", "job_profile_id": profile, "duration_minutes": 30}).json()["session_id"]
    topic = client.post("/interviews", json={"topic": "Kafka"}).json()["session_id"]
    article = tmp_path / "20260101-000000__kafka__deadbeef"
    article.mkdir()
    (article / "intermediate.md").write_text("# Kafka\n\nBody.")
    (article / "meta.json").write_text(json.dumps({
        "job_id": "deadbeef", "generated_at": "2026-01-01T00:00:00",
        "request": {"topic": "Kafka", "explanation_level": "intermediate"}}))
    return client, tmp_path, {"profile": profile, "job_session": session,
                              "topic_session": topic, "article": article.name,
                              "resume": "20260101-000000-aaa111"}


# ── What is held, and where it goes ──────────────────────────────────

def test_the_overview_counts_everything_and_shows_no_contents(store):
    client, _, _ = store

    body = client.get("/data").json()

    assert {k: v["count"] for k, v in body["stored"].items() if v["count"]} == {
        "resumes": 1, "job_targets": 1, "interviews": 2, "articles": 1}
    assert "Sam Okafor" not in json.dumps(body) and "Halberd" not in json.dumps(body)


def test_the_overview_does_not_let_local_storage_pass_for_local_processing(store, monkeypatch):
    client, _, _ = store
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")

    processed = client.get("/data").json()["processed_by"]

    assert processed["provider"] == "anthropic" and processed["local"] is False
    assert "does not mean processed locally" in processed["statement"]
    sent = {row["studio"]: row["sent"] for row in processed["what_is_sent"]}
    assert "full text of your resume" in sent["Resume"]
    assert "Audio" in sent["Voice"]
    assert "sk-ant" not in json.dumps(processed)


def test_the_overview_says_what_deleting_cannot_reach(store):
    client, _, _ = store

    limits = client.get("/data").json()["not_covered_by_delete"]

    assert any("provider retained" in line for line in limits)


# ── Export ───────────────────────────────────────────────────────────

def test_the_export_contains_every_record_exactly_as_stored(store):
    client, root, ids = store

    r = client.get("/data/export")
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(archive.namelist())

    assert r.headers["content-type"] == "application/zip"
    assert f"resumes/{ids['resume']}.json" in names
    assert f"job_profiles/{ids['profile']}.json" in names
    assert f"interviews/{ids['job_session']}.json" in names
    assert f"articles/{ids['article']}/intermediate.md" in names
    assert archive.read(f"resumes/{ids['resume']}.json") == \
        (root / "resumes" / f"{ids['resume']}.json").read_bytes()
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["counts"] == {"resumes": 1, "job_targets": 1, "interviews": 2, "articles": 1}


# ── Delete ───────────────────────────────────────────────────────────

def test_each_kind_of_record_can_be_deleted(store):
    client, root, ids = store

    assert client.delete(f"/resumes/{ids['resume']}").status_code == 200
    assert client.delete(f"/interviews/{ids['topic_session']}").status_code == 200
    assert client.delete(f"/articles/{ids['article']}").status_code == 200

    assert client.get(f"/resumes/{ids['resume']}").status_code == 404
    assert client.get(f"/interviews/{ids['topic_session']}").status_code == 404
    assert client.get(f"/articles/{ids['article']}").status_code == 404
    assert not (root / ids["article"]).exists()


def test_deleting_a_job_target_says_what_is_linked_to_it(store):
    client, _, ids = store

    body = client.delete(f"/job-profiles/{ids['profile']}").json()

    assert body == {"deleted": ids["profile"], "interviews_deleted": 0, "interviews_kept": 1}
    assert client.get(f"/interviews/{ids['job_session']}").status_code == 200


def test_a_job_target_can_be_deleted_with_its_interviews(store):
    client, _, ids = store

    body = client.delete(f"/job-profiles/{ids['profile']}?with_interviews=true").json()

    assert body["interviews_deleted"] == 1
    assert client.get(f"/interviews/{ids['job_session']}").status_code == 404
    assert client.get(f"/interviews/{ids['topic_session']}").status_code == 200, \
        "an interview that was not for this job is not touched"


@pytest.mark.parametrize("article_id", ["resumes", "interviews", "job_profiles", "_jobs", "..", "."])
def test_the_article_route_cannot_delete_a_record_folder(store, article_id):
    client, root, _ = store

    before = sorted(p.name for p in root.rglob("*"))

    # ".." and "." are rewritten by the client before they are sent, so
    # they reach a different route. What matters is the same either way.
    assert client.delete(f"/articles/{article_id}").status_code >= 400

    assert sorted(p.name for p in root.rglob("*")) == before, "nothing was removed"


def test_delete_everything_has_to_be_asked_for_in_words(store):
    client, _, _ = store

    for attempt in ({}, {"confirm": ""}, {"confirm": "yes"}, {"confirm": "true"}):
        assert client.post("/data/delete-all", json=attempt).status_code == 422

    assert client.get("/data").json()["stored"]["resumes"]["count"] == 1


def test_delete_everything_removes_records_and_keeps_settings(store, tmp_path):
    client, root, _ = store
    settings = tmp_path / "test-settings.env"
    settings.write_text("LLM_PROVIDER='anthropic'\n")

    r = client.post("/data/delete-all", json={"confirm": "delete everything"})

    assert r.status_code == 200
    stored = client.get("/data").json()["stored"]
    assert {k: v["count"] for k, v in stored.items()} == dict.fromkeys(stored, 0)
    remaining = [p.name for p in root.rglob("*") if p.is_file() and p != settings
                 and ".scrivio-state" not in p.parts and ".stage-cache" not in p.parts]
    assert remaining == [], f"left behind: {remaining}"
    assert settings.read_text() == "LLM_PROVIDER='anthropic'\n"


def test_delete_everything_waits_for_running_work(store, monkeypatch):
    client, _, ids = store
    monkeypatch.setattr(server, "_RESUME_WORK", {ids["resume"]})

    r = client.post("/data/delete-all", json={"confirm": "delete everything"})

    assert r.status_code == 409
    assert client.get(f"/resumes/{ids['resume']}").status_code == 200


# ── Backup and restore ───────────────────────────────────────────────

def test_a_backup_restores_into_an_empty_store(store, tmp_path, capsys):
    client, root, ids = store
    backup = tmp_path / "backup.zip"
    assert data_cli.main(["backup", str(backup)]) == 0
    assert oct(backup.stat().st_mode & 0o777) == "0o600", "a backup holds resumes"
    client.post("/data/delete-all", json={"confirm": "delete everything"})

    assert data_cli.main(["restore", str(backup)]) == 0

    doc = client.get(f"/resumes/{ids['resume']}").json()
    assert SECRET_LINE in doc["original_text"]
    assert client.get(f"/interviews/{ids['job_session']}").status_code == 200
    assert client.get(f"/articles/{ids['article']}").json()["markdown"].startswith("# Kafka")
    assert len(client.get("/articles").json()) == 1


def test_restore_does_not_overwrite_without_being_told_to(store, tmp_path):
    client, root, ids = store
    backup = data_controls.export_all(root)
    doc = server._load_resume_doc(ids["resume"])
    doc.jd_label = "edited after the backup"
    server._save_resume_doc(doc)

    result = data_controls.restore(root, backup)

    assert result["restored"] == 0 and result["skipped_existing"] > 0
    assert server._load_resume_doc(ids["resume"]).jd_label == "edited after the backup"


def test_a_replacing_restore_saves_what_was_there_first(store):
    client, root, ids = store
    backup = data_controls.export_all(root)
    doc = server._load_resume_doc(ids["resume"])
    doc.jd_label = "edited after the backup"
    server._save_resume_doc(doc)

    result = data_controls.restore(root, backup, replace=True)

    assert server._load_resume_doc(ids["resume"]).jd_label == ""
    saved = zipfile.ZipFile(result["saved_first"])
    before = json.loads(saved.read(f"resumes/{ids['resume']}.json"))
    assert before["jd_label"] == "edited after the backup", "the restore can be undone"


@pytest.mark.parametrize("member", [
    "../../outside.json", "/etc/cron.d/job", "resumes/../../escape.json",
    "somewhere_else/file.json", "..\\windows.json"])
def test_a_backup_cannot_write_outside_the_store(store, tmp_path, member):
    _, root, _ = store
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": 1, "application": "scrivio"}))
        archive.writestr(member, "{}")

    with pytest.raises(data_controls.RestoreRefused):
        data_controls.restore(root, out.getvalue())

    assert not (tmp_path.parent / "outside.json").exists()
    assert not (tmp_path / "escape.json").exists()


@pytest.mark.parametrize("content", [b"not a zip at all", b"PK\x03\x04 truncated"])
def test_a_file_that_is_not_a_backup_is_refused(store, content):
    _, root, _ = store

    with pytest.raises(data_controls.RestoreRefused):
        data_controls.restore(root, content)


def test_a_backup_from_a_newer_version_is_refused_with_a_reason(store):
    _, root, _ = store
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "format": data_controls.FORMAT + 1, "application": "scrivio"}))

    with pytest.raises(data_controls.RestoreRefused, match="newer version"):
        data_controls.restore(root, out.getvalue())


def test_a_record_saved_by_an_older_version_restores_and_opens(store):
    """The migration that matters most is the one that needs nothing done:
    every field added since has a default, so an old record just loads."""
    client, root, _ = store
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": 1, "application": "scrivio"}))
        archive.writestr("resumes/20250101-000000-0ld000.json", json.dumps({
            "resume_id": "20250101-000000-0ld000", "original_text": "Sam Okafor",
            "structured": {"basics": {"name": "Sam Okafor"}, "work": []}}))

    data_controls.restore(root, out.getvalue())

    doc = client.get("/resumes/20250101-000000-0ld000")
    assert doc.status_code == 200 and doc.json()["tailored_history"] == []


# ── Logs ─────────────────────────────────────────────────────────────

def test_a_failure_near_a_resume_does_not_put_the_resume_in_the_log(store, monkeypatch, caplog):
    """A validation error quotes the input it rejected. Logged with its
    traceback, that is the user's resume in a log file."""
    client, _, _ = store

    async def rejects(text, *args, **kwargs):
        from pipeline.schemas.models import ResumeReview
        ResumeReview.model_validate({"strengths": text, "issues": text})

    monkeypatch.setattr(server, "review_resume", rejects)
    monkeypatch.setattr(server, "extract_resume", rejects)

    with caplog.at_level(logging.DEBUG):
        created = client.post("/resumes", json={
            "resume_text": f"Sam Okafor\n{SECRET_LINE}\n" * 10}).json()
        final = client.get(f"/resumes/{created['resume_id']}").json()

    assert final["status"] == "error"
    logged = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert "failed" in logged, "the failure itself is still logged"
    assert "Halberd" not in logged and "Sam Okafor" not in logged
