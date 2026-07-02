"""Mechanical style checks on final article markdown.

These are cheap, deterministic scans that surface regressions without an LLM
call. They WARN, never rewrite: the prompts are responsible for preventing
these patterns; the checks make it visible when they fail so a prompt
regression is caught on the next generation instead of the next embarrassing
blog post.

The banned-phrase list lives in pipeline/prompts/_banned_phrases_v1.txt — the
same fragment injected into the drafter and polisher prompts — so the models'
instructions and this checker can never drift apart.
"""
from __future__ import annotations

import re

from pipeline.prompt_loader import PROMPTS_DIR

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _load_banned_phrases() -> tuple[str, ...]:
    """Parse the shared fragment: one phrase per '- ' line."""
    text = (PROMPTS_DIR / "_banned_phrases_v1.txt").read_text(encoding="utf-8")
    return tuple(
        line[2:].strip()
        for line in text.splitlines()
        if line.startswith("- ") and line[2:].strip()
    )


BANNED_PHRASES: tuple[str, ...] = _load_banned_phrases()


def find_banned_phrases(markdown: str) -> list[str]:
    """Case-insensitive banned-phrase scan over PROSE only.

    Fenced code blocks and inline code spans are excluded: `robust` is a
    legitimate identifier in someone's API even though it is banned prose.
    Returns the sorted unique list of phrases found.
    """
    prose = _FENCE_RE.sub(" ", markdown)
    prose = _INLINE_CODE_RE.sub(" ", prose)
    lowered = prose.lower()
    return sorted({p for p in BANNED_PHRASES if p.lower() in lowered})


def find_singlespace_indented_code_blocks(markdown: str) -> list[str]:
    """Flag fenced code blocks containing 1-space-indented lines.

    Real code indents by 2/4 spaces or tabs; lines indented by exactly one
    space are the signature of the historical whitespace-collapsing bug that
    flattened code blocks. Returns the first line of each offending block.
    """
    offenders: list[str] = []
    for block in _FENCE_RE.findall(markdown):
        body_lines = block.splitlines()[1:-1]  # drop the fence lines
        if any(re.match(r"^ \S", line) for line in body_lines):
            header = block.splitlines()[0] if block.splitlines() else "```"
            offenders.append(header)
    return offenders
