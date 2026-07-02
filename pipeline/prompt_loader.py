"""Prompt assembly with shared fragments.

Prompts live in pipeline/prompts/. Files starting with underscore are
fragments — shared blocks (voice rules, marker-preservation rules) that
multiple role prompts include via a directive line:

    {{include:_voice_canon_v1.txt}}

Why includes instead of copy-paste: the voice rules and banned-word lists
were previously duplicated across five prompts and had already drifted
apart (the editor flagged words the drafter never banned). One canonical
fragment keeps every writing and reviewing agent on the same rulebook.

NOTE for fragment authors: fragments must not contain literal `{` or `}` —
the compiler prompt is passed through str.format() after assembly, and any
stray brace in an included fragment would break its template variables.
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_INCLUDE_PATTERN = re.compile(r"\{\{include:([\w.-]+)\}\}")


def load_prompt(name: str) -> str:
    """Read a prompt file and expand {{include:...}} directives one level deep."""
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")

    def _expand(match: re.Match) -> str:
        fragment = (PROMPTS_DIR / match.group(1)).read_text(encoding="utf-8")
        return fragment.strip()

    return _INCLUDE_PATTERN.sub(_expand, text)
