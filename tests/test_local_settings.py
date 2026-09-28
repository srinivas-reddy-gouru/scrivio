"""The server and the tools beside it agree about where the data is (F04).

Each of these starts real processes. The defect was a disagreement
between two programs, and a test inside one process would have been a
test of one program.

Every path here is under a temporary directory. The repository's own
settings file is never read: where the default location is tested, the
process is told the repository is somewhere else.
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_server import LiveServer  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
KEPT = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "SYSTEMROOT")

# Prints what each program resolved, as JSON. `elsewhere` stands in for
# the repository when the default settings file is what is being tested.
SERVER = """
import json, sys
from pathlib import Path
from pipeline import local_settings
if sys.argv[1]:
    local_settings.REPO = Path(sys.argv[1])
import api.server as server
print(json.dumps({"root": str(Path(server.OUTPUT_ROOT).resolve()),
                  "settings": str(server._ENV_FILE)}))
"""
TOOL = """
import json, sys
from pathlib import Path
from pipeline import local_settings
if sys.argv[1]:
    local_settings.REPO = Path(sys.argv[1])
local_settings.load()
found = local_settings.resolve()
print(json.dumps({"root": str(found.data_root), "settings": str(found.settings_file),
                  "from": found.output_from, "demo": found.demo}))
"""


def environment(tmp_path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k in KEPT}
    env.update({"PYTHONPATH": str(REPO), "PYTHONWARNINGS": "ignore",
                "SCRIVIO_STATE_DIR": str(tmp_path / "state")})
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run(code_or_args, env, cwd=REPO, elsewhere="") -> subprocess.CompletedProcess:
    argv = ([sys.executable, "-c", code_or_args, str(elsewhere)]
            if isinstance(code_or_args, str) else [sys.executable, *code_or_args])
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def both(env, cwd=REPO, elsewhere="") -> tuple[dict, dict]:
    server, tool = run(SERVER, env, cwd, elsewhere), run(TOOL, env, cwd, elsewhere)
    assert server.returncode == 0, server.stderr[-800:]
    assert tool.returncode == 0, tool.stderr[-800:]
    return (json.loads(server.stdout.strip().splitlines()[-1]),
            json.loads(tool.stdout.strip().splitlines()[-1]))


def settings(path: Path, **values) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}='{v}'\n" for k, v in values.items()), encoding="utf-8")
    return path


# ── The reproduction ──────────────────────────────────────────────────

def test_the_case_from_the_follow_up_review(tmp_path):
    custom = tmp_path / "custom-output"
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=custom)

    server, tool = both(environment(tmp_path, SCRIVIO_ENV_FILE=file))

    assert server["root"] == str(custom.resolve())
    assert tool["root"] == server["root"]
    assert tool["from"] == "the settings file"


# ── Every way of configuring it ───────────────────────────────────────

def test_a_folder_named_only_in_the_environment(tmp_path):
    exported = tmp_path / "exported"

    server, tool = both(environment(
        tmp_path, SCRIVIO_ENV_FILE=tmp_path / "none.env", ARTICLE_OUTPUT_DIR=exported))

    assert tool["root"] == server["root"] == str(exported.resolve())
    assert tool["from"] == "the environment"


def test_the_settings_file_wins_over_the_environment_in_both(tmp_path):
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=tmp_path / "from-file")

    server, tool = both(environment(
        tmp_path, SCRIVIO_ENV_FILE=file, ARTICLE_OUTPUT_DIR=tmp_path / "exported"))

    assert tool["root"] == server["root"] == str((tmp_path / "from-file").resolve())


def test_the_settings_file_in_its_default_place(tmp_path):
    """The default is `.env` beside the code. The processes are told the
    code is in a temporary directory, so the real one is not read."""
    elsewhere = tmp_path / "checkout"
    settings(elsewhere / ".env", ARTICLE_OUTPUT_DIR=tmp_path / "default-file")

    server, tool = both(environment(tmp_path), elsewhere=elsewhere)

    assert server["settings"] == tool["settings"] == str(elsewhere / ".env")
    assert tool["root"] == server["root"] == str((tmp_path / "default-file").resolve())


def test_nothing_configured_at_all(tmp_path):
    server, tool = both(
        environment(tmp_path, SCRIVIO_ENV_FILE=tmp_path / "none.env"), cwd=tmp_path)

    assert tool["root"] == server["root"] == str((tmp_path / "output").resolve())
    assert tool["from"] == "the default"


@pytest.mark.parametrize("where", ["file", "environment"])
def test_demo_mode_has_its_own_folder_in_both(tmp_path, where):
    custom = tmp_path / "custom-output"
    if where == "file":
        file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=custom, SCRIVIO_DEMO="1")
        env = environment(tmp_path, SCRIVIO_ENV_FILE=file)
    else:
        file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=custom)
        env = environment(tmp_path, SCRIVIO_ENV_FILE=file, SCRIVIO_DEMO="1")

    server, tool = both(env)

    assert tool["root"] == server["root"] == str((custom / "demo-mode").resolve())
    assert tool["demo"] is True


def test_a_relative_folder_is_relative_to_where_the_program_was_started(tmp_path):
    """Documented, and unchanged. Started from the same place, both see
    the same folder. The tool prints the full path, so that being
    started from somewhere else shows."""
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR="records")
    env = environment(tmp_path, SCRIVIO_ENV_FILE=file)
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir(), there.mkdir()

    server, tool = both(env, cwd=here)
    _, from_there = both(env, cwd=there)

    assert tool["root"] == server["root"] == str((here / "records").resolve())
    assert from_there["root"] == str((there / "records").resolve())


def test_a_value_with_a_dollar_and_braces_is_not_expanded(tmp_path):
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=tmp_path / "out-${HOME}")

    server, tool = both(environment(tmp_path, SCRIVIO_ENV_FILE=file))

    assert tool["root"] == server["root"]
    assert tool["root"].endswith("out-${HOME}")


def test_importing_the_module_reads_nothing(tmp_path):
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=tmp_path / "from-file")
    probe = ("import os; from pipeline import local_settings; "
             "print(os.environ.get('ARTICLE_OUTPUT_DIR', 'unset'))")

    done = run(probe, environment(tmp_path, SCRIVIO_ENV_FILE=file))

    assert done.stdout.strip().splitlines()[-1] == "unset"


def test_the_tool_does_not_start_the_application_to_find_a_folder(tmp_path):
    probe = ("import sys; from api import data; "
             "print('api.server' in sys.modules, 'main' in sys.modules)")

    done = run(probe, environment(tmp_path, SCRIVIO_ENV_FILE=tmp_path / "none.env"))

    assert done.stdout.strip().splitlines()[-1] == "False False"


# ── The tool says where it is looking ─────────────────────────────────

def test_where_prints_the_file_the_folder_and_why(tmp_path):
    custom = tmp_path / "custom-output"
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=custom)

    done = run(["-m", "api.data", "where"], environment(tmp_path, SCRIVIO_ENV_FILE=file))

    assert done.returncode == 0, done.stderr
    assert str(file) in done.stdout
    assert str(custom.resolve()) in done.stdout
    assert "set by the settings file" in done.stdout
    assert "Records        0" in done.stdout


def test_a_settings_file_that_is_not_there_is_said_to_be_not_there(tmp_path):
    done = run(["-m", "api.data", "where"],
               environment(tmp_path, SCRIVIO_ENV_FILE=tmp_path / "none.env"), cwd=tmp_path)

    assert "not there" in done.stdout


def test_an_empty_folder_is_not_backed_up_without_saying_so(tmp_path):
    """A backup of the wrong folder is a well-formed zip. It is refused
    when there is nothing in it, which is what the wrong folder has."""
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=tmp_path / "empty")
    env = environment(tmp_path, SCRIVIO_ENV_FILE=file)
    target = tmp_path / "backup.zip"

    refused = run(["-m", "api.data", "backup", str(target)], env)

    assert refused.returncode == 1
    assert "there are no records in" in refused.stderr and not target.exists()
    assert run(["-m", "api.data", "backup", str(target), "--allow-empty"], env).returncode == 0
    assert target.exists()


# ── One record, from the server to a backup and back ──────────────────

RESUME = ("Jordan Rivera\nBackend Engineer\njordan@example.com | +1 555 010 1234\n\n"
          "Experience\nSoftware Engineer, Acme Corp\nJan 2021 - Present\n"
          "- Built Kafka pipelines processing 2M events/day\n\nSkills\nPython, Kafka\n")


class ConfiguredByFile(LiveServer):
    """A server that learns its data folder from its settings file and
    from nowhere else, which is the arrangement the defect needed."""

    def __init__(self, root: Path, file: Path):
        super().__init__(root, demo=True)
        self.file = file

    def _environment(self) -> dict[str, str]:
        env = super()._environment()
        env.pop("ARTICLE_OUTPUT_DIR", None)
        env["SCRIVIO_ENV_FILE"] = str(self.file)
        return env


def test_a_record_the_server_wrote_is_in_the_backup_and_comes_back(tmp_path):
    custom = tmp_path / "custom-output"
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=custom)
    tool = environment(tmp_path, SCRIVIO_ENV_FILE=file, SCRIVIO_DEMO="1")
    backup = tmp_path / "backup.zip"

    server = ConfiguredByFile(tmp_path / "server", file).start()
    try:
        status, made = server.json("POST", "/resumes", {"resume_text": RESUME})
        assert status == 200, made
        resume_id = made["resume_id"]
    finally:
        server.stop()
    written = custom / "demo-mode" / "resumes" / f"{resume_id}.json"
    assert written.is_file(), "the server kept the record where its settings file said"

    saved = run(["-m", "api.data", "backup", str(backup)], tool)
    assert saved.returncode == 0, saved.stderr
    assert f"from {written.parents[1].resolve()}" in saved.stdout
    with zipfile.ZipFile(backup) as archive:
        assert any(name.endswith(f"resumes/{resume_id}.json") for name in archive.namelist())

    written.unlink()
    restored = run(["-m", "api.data", "restore", str(backup)], tool)
    assert restored.returncode == 0, restored.stderr
    assert f"Restoring into {written.parents[1].resolve()}" in restored.stdout
    assert written.is_file()

    server = ConfiguredByFile(tmp_path / "server", file).start()
    try:
        status, listed = server.json("GET", "/resumes")
        assert status == 200 and [r["resume_id"] for r in listed] == [resume_id]
    finally:
        server.stop()


def test_the_setup_check_reports_the_same_folder(tmp_path):
    custom = tmp_path / "custom-output"
    file = settings(tmp_path / "settings.env", ARTICLE_OUTPUT_DIR=custom)

    done = run(["-m", "api.doctor"], environment(tmp_path, SCRIVIO_ENV_FILE=file))

    assert str(custom.resolve()) in done.stdout
