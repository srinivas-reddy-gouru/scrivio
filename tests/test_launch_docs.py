"""Launch materials describe what exists (R25).

A demonstration script rots the day a button is renamed. These hold the
documents to the code, and check that the documents do not claim things
that have not happened.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "docs" / "launch"
PUBLIC = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CONTRIBUTING.md",
          ROOT / "SECURITY.md", *sorted(LAUNCH.glob("*.md"))]


def _tests_defined() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        found |= set(re.findall(r"^def (test_\w+)", path.read_text(encoding="utf-8"), re.M))
    return found


def test_every_step_of_the_demonstration_names_a_test_that_exists():
    script = (LAUNCH / "demo-script.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`(test_\w+)`", script))

    assert len(named) >= 4
    assert named - _tests_defined() == set()


def test_every_control_the_demonstration_uses_is_in_the_interface():
    script = (LAUNCH / "demo-script.md").read_text(encoding="utf-8")
    interface = "\n".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "web" / "src").rglob("*.tsx"))
    steps = script.split("## Steps", 1)[1].split("## If asked", 1)[0]

    for control in re.findall(r"\*\*([^*]+)\*\*", steps):
        assert control in interface, control


def test_every_issue_draft_points_at_something_that_exists():
    drafts = (LAUNCH / "issue-drafts.md").read_text(encoding="utf-8")

    for path in set(re.findall(r"`((?:pipeline|api|tests|evals|web)/[\w./-]+\.\w+)`", drafts)):
        if path.startswith("api/routes/"):
            continue                       # the file the draft proposes to create
        assert (ROOT / path).is_file(), path
    for name in ("quantities_in", "_TOKEN_RE"):
        assert name in (ROOT / "pipeline/workers/resume_fact_guard.py").read_text()
    for case in re.findall(r"case `([\w-]+)`", drafts):
        assert f'"id": "{case}"' in (
            ROOT / "evals/corpus/v1/resume_cases.json").read_text(encoding="utf-8"), case


def test_every_file_the_launch_index_lists_is_there():
    index = (LAUNCH / "README.md").read_text(encoding="utf-8")

    for name in re.findall(r"\]\(([\w-]+\.md)\)", index):
        assert (LAUNCH / name).is_file(), name


@pytest.mark.parametrize("path", PUBLIC, ids=lambda p: p.name)
def test_nothing_public_carries_a_dash_the_project_does_not_use(path):
    text = path.read_text(encoding="utf-8")

    assert "—" not in text and "–" not in text


@pytest.mark.parametrize("path", PUBLIC, ids=lambda p: p.name)
def test_nothing_public_makes_a_claim_that_was_withdrawn(path):
    """Phrases removed in R23. One may appear where a document is saying
    NOT to use it, which is the table in the project description."""
    text = path.read_text(encoding="utf-8").lower()
    if path.name == "project-description.md":
        text = text.split("## what not to say", 1)[0] + text.split("## topics", 1)[1]

    for phrase in ("recruiter-grade", "refuses to invent a single fact",
                   "never writes a word that is not true", "grading you can trust",
                   "zero api cost", "every claim verified", "every claim is checked"):
        assert phrase not in text, phrase


def test_the_launch_documents_say_that_nothing_has_happened_yet():
    said = {
        "README.md": "Prepared, not published",
        "issue-drafts.md": "None has been filed",
        "ten-user-protocol.md": "No sessions have been held",
        "measurement-sheet.md": "There are no numbers yet",
        "release-checklist.md": "Nothing here has been done",
    }
    for name, phrase in said.items():
        assert phrase in (LAUNCH / name).read_text(encoding="utf-8"), name
    assert "Nothing has been released" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "- [x]" not in (LAUNCH / "release-checklist.md").read_text(encoding="utf-8").lower()


def test_no_launch_document_contains_the_owners_details():
    import subprocess

    owner = subprocess.run(
        ["git", "config", "user.email"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    for path in PUBLIC:
        if owner:
            assert owner not in path.read_text(encoding="utf-8"), path.name
