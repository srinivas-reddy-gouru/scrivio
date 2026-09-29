"""Prompts live in files, and the files are where the code says they are.

The project rule is that every prompt template lives in pipeline/prompts/
with a version suffix. These tests read the source rather than run it, so
they cost nothing and call no model.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "pipeline" / "prompts"
SOURCES = sorted(
    p for top in ("pipeline", "api", "render") for p in (ROOT / top).rglob("*.py")
) + [ROOT / "main.py"]

# Written before the rule settled on one suffix. Each is the second version
# of an article-pipeline prompt and is named for what it is. Renaming them
# is the owner's decision (see docs/PROMPTS.md); until then the list is
# closed, so that nothing new joins it unnoticed.
LEGACY_NAMES = frozenset({
    "brief_v2.txt", "compiler_v2.txt", "critic_v2.txt", "diagram_spec_v2.txt",
    "drafter_v2.txt", "editor_v2.txt", "planner_v2.txt", "polisher_v2.txt",
    "relevance_checker_v2.txt", "verifier_v2.txt", "vhs_tape_v2.txt",
})


def _is_literal_text(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and len(node.value.split()) > 6
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_literal_text(node.left) or _is_literal_text(node.right)
    return False


def _inline_system_prompts(path: Path) -> list[int]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "system" and _is_literal_text(keyword.value):
                    found.append(keyword.value.lineno)
        if isinstance(node, ast.Dict):
            pairs = {
                k.value: v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant)
            }
            role = pairs.get("role")
            if (isinstance(role, ast.Constant) and role.value == "system"
                    and "content" in pairs and _is_literal_text(pairs["content"])):
                found.append(node.lineno)
    return found


def test_no_system_prompt_is_written_inline():
    inline = {
        str(path.relative_to(ROOT)): lines
        for path in SOURCES if (lines := _inline_system_prompts(path))
    }
    assert inline == {}


def test_every_prompt_the_code_names_exists():
    named = set()
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        named |= set(re.findall(r'load_prompt\(\s*"([\w.-]+)"', text))
        named |= set(re.findall(r'"([\w-]+_v\d+\.txt)"', text))
    missing = sorted(name for name in named if not (PROMPTS / name).is_file())
    assert named, "found no prompt references at all, so this test checks nothing"
    assert missing == []


def test_every_prompt_file_is_used():
    text = "\n".join(p.read_text(encoding="utf-8") for p in SOURCES)
    fragments = "\n".join(p.read_text(encoding="utf-8") for p in PROMPTS.glob("*.txt"))
    unused = sorted(
        p.name for p in PROMPTS.glob("*.txt")
        if p.name not in text and f"include:{p.name}" not in fragments
    )
    assert unused == []


def test_new_prompts_carry_the_v1_suffix():
    other = {p.name for p in PROMPTS.glob("*.txt") if not p.name.endswith("_v1.txt")}
    assert other == LEGACY_NAMES


@pytest.mark.parametrize("name", [
    "official_sources_v1.txt", "topic_classifier_v1.txt",
    "resume_summary_condenser_v1.txt", "search_queries_v1.txt",
    "workflow_search_queries_v1.txt", "claim_search_query_v1.txt",
])
def test_a_moved_prompt_has_no_directive_or_stray_whitespace(name):
    """These six were string literals until they were moved. What is sent
    must be what was sent before: no include directive to expand, and the
    single trailing newline is stripped where the file is loaded."""
    raw = (PROMPTS / name).read_text(encoding="utf-8")
    assert "{{include" not in raw
    assert raw == raw.strip() + "\n"


def test_the_condenser_still_forbids_additions_and_dashes():
    text = (PROMPTS / "resume_summary_condenser_v1.txt").read_text(encoding="utf-8")
    assert "Never add a skill, title, or claim" in text
    assert "No em or en dashes" in text


def test_the_search_query_prompt_takes_a_count_and_nothing_else():
    """It is filled in with str.format, so any other brace would raise at
    the moment of the call, in the middle of a paid run."""
    raw = (PROMPTS / "search_queries_v1.txt").read_text(encoding="utf-8")
    assert re.findall(r"[{}][^{}]*[{}]?", raw) == ["{count}"]
    assert "exactly 4 targeted" in raw.format(count=4)
