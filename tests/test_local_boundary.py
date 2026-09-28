"""The local application boundary (review item R04).

Every other test file runs as a paired browser, because that is what a
test of resume tailoring is about. This file is about the door itself,
so it takes the conftest shortcut away and meets the real checks.
"""
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import boundary, server
from api.boundary import request_is_authenticated as REAL_CHECK
from pipeline.schemas.models import ResumeDoc, StructuredResume

RESUME_ID = "20260101-000000-abc123"
LOCAL = {"host": "127.0.0.1:8899"}


@pytest.fixture
def door(tmp_path, monkeypatch):
    """The real boundary, a private state directory, one synthetic resume."""
    monkeypatch.setattr(boundary, "request_is_authenticated", REAL_CHECK)
    monkeypatch.setenv("SCRIVIO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SCRIVIO_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("SCRIVIO_ALLOWED_ORIGINS", raising=False)
    monkeypatch.setattr(boundary, "pairing", boundary.Pairing())
    monkeypatch.setattr(server, "OUTPUT_ROOT", tmp_path / "out")
    # The settings endpoint writes a real file. It must never be the
    # developer's own .env, so this name is patched without raising=False:
    # if the server renames it, this fixture fails loudly instead of
    # quietly letting a test write to the real thing.
    monkeypatch.setattr(server, "_ENV_FILE", tmp_path / "settings.env")
    server._save_resume_doc(ResumeDoc(
        resume_id=RESUME_ID, original_text="Sam Okafor",
        structured=StructuredResume.model_validate({"basics": {"name": "Sam Okafor"}})))
    return TestClient(server.app, headers=LOCAL)


def _pair(client) -> None:
    r = client.post("/auth/pair", json={"code": boundary.pairing.code})
    assert r.status_code == 200, r.text


def _every_api_route():
    """(method, path) for every route the application declares, so a route
    added later is covered by these tests without anyone remembering to."""
    for route in server.app.router.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD"}):
                yield method, re.sub(r"\{[^}]+\}", RESUME_ID, route.path)


PROTECTED = [(m, p) for m, p in _every_api_route() if p not in boundary.PUBLIC_PATHS]


def test_the_route_inventory_covers_every_family():
    families = {p.split("/")[1] for _, p in PROTECTED}
    assert {"resumes", "settings", "articles", "interviews", "job-profiles",
            "jobs", "generate", "clarify", "transcribe", "speak"} <= families


@pytest.mark.parametrize("method, path", PROTECTED)
def test_an_anonymous_caller_is_refused_on_every_route(door, method, path):
    r = door.request(method, path, json={})

    assert r.status_code == 401, f"{method} {path} answered an anonymous caller"
    assert "Sam" not in r.text


def test_an_anonymous_caller_cannot_read_export_or_delete_a_resume(door):
    """The reproduced case, spelled out."""
    assert door.get("/resumes").status_code == 401
    assert door.get(f"/resumes/{RESUME_ID}").status_code == 401
    assert door.get(f"/resumes/{RESUME_ID}/download?fmt=md").status_code == 401
    assert door.delete(f"/resumes/{RESUME_ID}").status_code == 401
    assert server._resume_path(RESUME_ID).is_file(), "the resume must still exist"


def test_an_anonymous_caller_cannot_change_settings(door, tmp_path):
    r = door.patch("/settings", json={"updates": {"LLM_PROVIDER": "openai"}})

    assert r.status_code == 401
    assert not (tmp_path / "settings.env").exists()


def test_the_api_documentation_is_not_public_either(door):
    assert door.get("/openapi.json").status_code == 401
    assert door.get("/docs").status_code == 401


def test_health_answers_anyone_and_says_nothing(door):
    r = door.get("/health")

    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ── Pairing ──────────────────────────────────────────────────────────

def test_pairing_with_the_terminal_code_opens_the_api(door):
    assert door.get("/auth/status").json()["authenticated"] is False

    _pair(door)

    assert door.get("/auth/status").json()["authenticated"] is True
    assert door.get("/resumes").status_code == 200
    assert door.get(f"/resumes/{RESUME_ID}/download?fmt=md").status_code == 200


def test_the_session_cookie_is_out_of_reach_of_scripts_and_other_sites(door):
    r = door.post("/auth/pair", json={"code": boundary.pairing.code})

    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert boundary.pairing.code not in r.text, "the code is never echoed back"


def test_a_pairing_code_works_once(door):
    code = boundary.pairing.code
    _pair(door)
    stranger = TestClient(server.app, headers=LOCAL)

    assert stranger.post("/auth/pair", json={"code": code}).status_code == 403


def test_a_wrong_code_is_refused_and_guessing_is_locked_out(door):
    for _ in range(5):
        assert door.post("/auth/pair", json={"code": "AAAA-AAAA"}).status_code == 403

    locked = door.post("/auth/pair", json={"code": boundary.pairing.code})

    assert locked.status_code == 429, "even the right code waits out the lockout"
    assert door.get("/resumes").status_code == 401


def test_a_forged_or_expired_session_is_refused(door):
    _pair(door)
    good = door.cookies.get(boundary.COOKIE_NAME)
    payload, _, signature = good.rpartition(".")

    forged = TestClient(server.app, headers=LOCAL,
                        cookies={boundary.COOKIE_NAME: f"{payload}.{signature[::-1]}"})
    assert forged.get("/resumes").status_code == 401
    far_future = __import__("time").time() + (boundary.SESSION_DAYS + 1) * 86400
    assert not boundary.session_is_valid(good, now=far_future)


def test_signing_out_and_forgetting_every_browser(door):
    _pair(door)
    other = TestClient(server.app, headers=LOCAL,
                       cookies={boundary.COOKIE_NAME: door.cookies.get(boundary.COOKIE_NAME)})
    assert other.get("/resumes").status_code == 200

    assert door.post("/auth/forget-all").status_code == 200

    assert other.get("/resumes").status_code == 401, "the old session must stop working"
    assert door.get("/resumes").status_code == 200, "the browser that asked stays paired"
    door.post("/auth/logout")
    assert door.get("/resumes").status_code == 401


def test_the_session_key_is_readable_only_by_its_owner(door, tmp_path):
    _pair(door)

    key = tmp_path / "state" / "session.key"
    assert key.is_file()
    assert oct(key.stat().st_mode & 0o777) == "0o600"


# ── Host and origin ──────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:8899", "192.168.1.20:8899", "127.0.0.1.evil.example", ""])
def test_a_request_addressed_to_another_host_name_is_refused(door, host):
    """DNS rebinding: a hostile page resolves its own name to 127.0.0.1.
    The connection arrives on loopback, the Host header tells the truth."""
    _pair(door)

    r = door.get("/resumes", headers={"host": host})

    assert r.status_code == 400


@pytest.mark.parametrize("host", ["localhost:8899", "127.0.0.1:8899", "[::1]:8899", "localhost"])
def test_loopback_names_are_accepted(door, host):
    _pair(door)

    assert door.get("/resumes", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://evil.example:8899", "null"])
def test_a_paired_browser_cannot_be_driven_from_another_site(door, origin):
    """The cookie would be attached by a browser without SameSite. This is
    the check that holds even then."""
    _pair(door)

    read = door.get("/resumes", headers={"origin": origin})
    write = door.delete(f"/resumes/{RESUME_ID}", headers={"origin": origin})

    assert read.status_code == 403 and write.status_code == 403
    assert server._resume_path(RESUME_ID).is_file()


def test_a_cross_site_form_post_without_an_origin_header_is_refused(door):
    _pair(door)

    r = door.delete(f"/resumes/{RESUME_ID}", headers={"sec-fetch-site": "cross-site"})

    assert r.status_code == 403
    assert server._resume_path(RESUME_ID).is_file()


def test_the_applications_own_origin_is_accepted(door):
    _pair(door)

    r = door.post(f"/resumes/{RESUME_ID}/undo-tailored",
                  headers={"origin": "http://127.0.0.1:8899", "sec-fetch-site": "same-origin"})

    assert r.status_code != 403 and r.status_code != 401


def test_no_wildcard_cors(door):
    _pair(door)

    r = door.get("/resumes", headers={"origin": "http://localhost:8899"})

    assert r.headers.get("access-control-allow-origin") != "*"


def test_an_extra_host_has_to_be_named_explicitly(door, monkeypatch):
    _pair(door)
    assert door.get("/resumes", headers={"host": "scrivio.lan"}).status_code == 400

    monkeypatch.setenv("SCRIVIO_ALLOWED_HOSTS", "scrivio.lan")

    assert door.get("/resumes", headers={"host": "scrivio.lan"}).status_code == 200
