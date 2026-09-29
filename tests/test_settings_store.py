"""Settings validation and persistence (review item R10).

Every file in this test module is a temporary one. conftest also points
the server's settings file at a temp path for every test in the suite,
so a mistake here still cannot reach the developer's real .env.
"""
import os
import stat
import threading

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from api import server, settings_store
from api.settings_store import SettingsError


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    monkeypatch.setattr(server, "_ENV_FILE", path)
    for key in ("ANTHROPIC_STRONG_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "USE_JINA_READER", "LLM_PROVIDER", "LLM_CLI", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return path


def patch(updates: dict):
    return TestClient(server.app).patch("/settings", json={"updates": updates})


def as_loaded_at_startup(path):
    """What the server would hold after a restart."""
    return dict(dotenv_values(path, interpolate=False))


# ── The reproduced case ──────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "claude-x\nOPENAI_API_KEY=attacker-controlled",
    "claude-x\r\nOPENAI_API_KEY=attacker-controlled",
    "claude-x\rOPENAI_API_KEY=attacker-controlled",
    "claude-x OPENAI_API_KEY=attacker-controlled",
    "claude-x\x00", "claude-x\x1b[31m", "claude\x85x",
])
def test_a_value_with_a_line_break_or_control_character_is_refused(env_file, value):
    env_file.write_text("TAVILY_API_KEY='existing-key-12345'\n")
    before = env_file.read_text()

    r = patch({"ANTHROPIC_STRONG_MODEL": value})

    assert r.status_code == 422
    assert env_file.read_text() == before, "a refused save must not touch the file"
    assert "OPENAI_API_KEY" not in os.environ
    assert "attacker" not in r.text and "claude-x" not in r.text, \
        "the error must not repeat the value it refused"
    assert "ANTHROPIC_STRONG_MODEL" in r.json()["detail"]


def test_one_bad_value_refuses_the_whole_save(env_file):
    r = patch({"ANTHROPIC_STRONG_MODEL": "claude-opus-4-7",
               "OPENAI_STRONG_MODEL": "gpt\nX=1"})

    assert r.status_code == 422
    assert not env_file.exists()
    assert "ANTHROPIC_STRONG_MODEL" not in os.environ, "nothing is half applied"


# ── What each kind of setting accepts ────────────────────────────────

@pytest.mark.parametrize("key, value, stored", [
    ("USE_JINA_READER", "TRUE", "true"), ("USE_JINA_READER", "no", "false"),
    ("LLM_PROVIDER", "OpenAI", "openai"), ("LLM_PROVIDER", "claude-cli", "claude-cli"),
    ("ANTHROPIC_STRONG_MODEL", "claude-opus-4-7", "claude-opus-4-7"),
    ("OPENAI_LIGHT_MODEL", "gpt-5.4-mini", "gpt-5.4-mini"),
    ("CLI_STRONG_MODEL", "llama3:8b", "llama3:8b"),
    ("CLI_STRONG_MODEL", "us.anthropic.claude-sonnet-4-6@20260101", "us.anthropic.claude-sonnet-4-6@20260101"),
    ("CLI_FORCE_MODEL", "org/model-name", "org/model-name"),
    ("OPENAI_API_KEY", "sk-proj-Ab3$x#y'z\"9_-+=/", "sk-proj-Ab3$x#y'z\"9_-+=/"),
])
def test_legitimate_values_are_accepted_and_survive_a_restart(env_file, key, value, stored):
    r = patch({key: value})

    assert r.status_code == 200, r.text
    assert as_loaded_at_startup(env_file)[key] == stored
    assert os.environ[key] == stored


@pytest.mark.parametrize("key, value", [
    ("USE_JINA_READER", "maybe"), ("USE_JINA_READER", "2"),
    ("LLM_PROVIDER", "azure"), ("LLM_CLI", "not-a-cli"),
    ("ANTHROPIC_STRONG_MODEL", "claude opus"), ("ANTHROPIC_STRONG_MODEL", "model; rm -rf"),
    ("OPENAI_LIGHT_MODEL", "$(whoami)"), ("OPENAI_LIGHT_MODEL", "`id`"),
    ("OPENAI_API_KEY", "short"), ("OPENAI_API_KEY", "has a space in it"),
    ("ANTHROPIC_API_KEY", "x" * 2000),
])
def test_values_of_the_wrong_shape_are_refused(env_file, key, value):
    r = patch({key: value})

    assert r.status_code == 422
    assert not env_file.exists()
    assert key not in os.environ


def test_an_unknown_setting_is_refused_without_echoing_it(env_file):
    r = patch({"PATH": "/tmp/evil", "LD_PRELOAD": "/tmp/x.so"})

    assert r.status_code == 400
    assert not env_file.exists()
    assert os.environ.get("LD_PRELOAD") != "/tmp/x.so"


# ── Round trips ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "plain", "two words", "  padded  ", "with # hash", "#leading-hash",
    "dollar $HOME sign", "braces ${HOME} here", "$(command)", "`backticks`",
    "single ' quote", 'double " quote', "both ' and \"", "back\\slash", "trailing\\",
    "equals=inside", "semi;colon", "unicode é ü 日本", "'already quoted'",
    "export KEY=value",
])
def test_any_single_line_value_reads_back_exactly(tmp_path, value):
    """The store itself, below the per-setting rules: whatever is written
    is what a restart reads. No comment, no expansion, no lost quote."""
    path = tmp_path / "settings.env"

    settings_store.update(path, {"SOME_SETTING": value}, managed={"SOME_SETTING"})

    assert settings_store.read(path) == {"SOME_SETTING": value}
    assert as_loaded_at_startup(path) == {"SOME_SETTING": value}


def test_a_secret_is_not_expanded_as_a_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_SECRET", "leaked")
    path = tmp_path / "settings.env"

    settings_store.update(path, {"API_TOKEN": "abc${HOME_SECRET}def"}, managed={"API_TOKEN"})

    assert settings_store.read(path)["API_TOKEN"] == "abc${HOME_SECRET}def"


def test_clearing_a_setting_stays_cleared_after_a_restart(env_file):
    patch({"ANTHROPIC_STRONG_MODEL": "claude-opus-4-7"})
    assert as_loaded_at_startup(env_file)["ANTHROPIC_STRONG_MODEL"] == "claude-opus-4-7"

    r = patch({"ANTHROPIC_STRONG_MODEL": ""})

    assert r.status_code == 200
    assert "ANTHROPIC_STRONG_MODEL" not in as_loaded_at_startup(env_file)
    assert "ANTHROPIC_STRONG_MODEL" not in os.environ


def test_clearing_removes_every_spelling_of_the_line(tmp_path):
    """`export KEY=value` and a duplicate are the same setting. Leaving
    either behind would bring the old value back at the next start."""
    path = tmp_path / "settings.env"
    path.write_text("export LLM_PROVIDER=openai\nLLM_PROVIDER='anthropic'\n")

    settings_store.update(path, {"LLM_PROVIDER": ""}, managed={"LLM_PROVIDER"})

    assert "LLM_PROVIDER" not in as_loaded_at_startup(path)


def test_lines_the_application_does_not_manage_are_left_alone(tmp_path):
    path = tmp_path / "settings.env"
    untouched = (
        "# my notes\n\nMY_OWN_VAR=keep me\n"
        'MULTILINE="first line\nOPENAI_API_KEY=looks like an assignment\nlast line"\n'
        "export ANOTHER=1\n")
    path.write_text(untouched + "LLM_PROVIDER=openai\n")

    settings_store.update(path, {"LLM_PROVIDER": "anthropic"},
                          managed={"LLM_PROVIDER", "OPENAI_API_KEY"})

    text = path.read_text()
    assert text.startswith(untouched), "unmanaged lines must be copied through unchanged"
    loaded = as_loaded_at_startup(path)
    assert loaded["LLM_PROVIDER"] == "anthropic"
    assert "OPENAI_API_KEY" not in loaded, \
        "a line inside someone's multi-line value is not an assignment"


# ── The file itself ──────────────────────────────────────────────────

def test_the_file_is_readable_only_by_its_owner(env_file):
    patch({"OPENAI_API_KEY": "sk-test-not-a-real-key"})

    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_a_failed_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    path.write_text("LLM_PROVIDER='openai'\n")

    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(settings_store.os, "replace", broken_replace)

    with pytest.raises(OSError):
        settings_store.update(path, {"LLM_PROVIDER": "anthropic"}, managed={"LLM_PROVIDER"})

    assert path.read_text() == "LLM_PROVIDER='openai'\n"
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == [], \
        "no temporary file holding secrets is left behind"


def test_a_failed_write_does_not_change_the_running_server(env_file, monkeypatch):
    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(settings_store.os, "replace", broken_replace)

    r = patch({"ANTHROPIC_STRONG_MODEL": "claude-opus-4-7"})

    assert r.status_code == 500
    assert "ANTHROPIC_STRONG_MODEL" not in os.environ
    assert "disk full" not in r.text and "claude-opus" not in r.text


def test_concurrent_saves_do_not_corrupt_the_file(tmp_path):
    path = tmp_path / "settings.env"
    keys = [f"SETTING_{i:02d}" for i in range(24)]
    errors = []

    def save(key):
        try:
            for round_ in range(5):
                settings_store.update(path, {key: f"value-{round_}"}, managed=set(keys))
        except Exception as exc:          # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert settings_store.read(path) == {k: "value-4" for k in keys}
    assert len(path.read_text().splitlines()) == len(keys), "no duplicate or torn lines"


def test_errors_name_the_setting_and_never_the_value():
    with pytest.raises(SettingsError) as refused:
        settings_store.check_value("OPENAI_API_KEY", "sk-very-secret-value with space", kind="secret")

    assert "OPENAI_API_KEY" in str(refused.value)
    assert "very-secret" not in str(refused.value)
    assert not hasattr(refused.value, "value")
