"""Central model-selection configuration.

Three presets, one helper:
  balanced  (default) — Haiku for light routing tasks; Sonnet for writing.
  best                — Sonnet everywhere; maximum quality.
  fast                — Haiku wherever safe; Sonnet only for the core writer.

Workers call get_model(role, preset) instead of hard-coding a model string
so users can influence cost vs. quality from the UI without touching code.
"""
from __future__ import annotations

import os

# Roles understood by the pipeline.
# Adding a new worker? Add a role here and fill in all three presets below.
# NOTE: "verify" uses OpenAI (gpt-4o-mini), not Anthropic — it is NOT
# controlled by this preset. The preset only covers Anthropic workers.
ROLES = (
    "brief",
    "relevance",
    "planning",
    "drafting",
    "editor",
    "polish",
    "critic",
    "closing",      # tiny Haiku call to close a truncated article
    "diagram",         # Mermaid spec generation — structured, not prose
    "diagram_review",  # diagram ↔ section-text semantic check (tiny JSON call)
    "sources",         # topic → official-docs domain resolution (tiny JSON call)
    "interviewer",     # interview-question + rubric generation (practice mode)
    "evaluator",       # grades candidate answers against the rubric
    "resume_extract",  # resume text → JSON Resume structure (faithful mapping)
    "resume_review",   # recruiter-lens resume critique — judgment, keep strong
    "resume_tailor",   # JD-targeted rewrite — the honesty-critical call
)

# Model aliases — update these when Anthropic releases new models.
_SONNET = "claude-sonnet-4-6"
_HAIKU  = "claude-haiku-4-5-20251001"

# ── Preset definitions ──────────────────────────────────────────────────────
# Each preset maps role → model string.
# "balanced" reproduces what each worker was previously hard-coded to use.

_PRESETS: dict[str, dict[str, str]] = {
    "balanced": {
        "brief":     _SONNET,   # short but quality-sensitive (angle + thesis)
        "relevance": _HAIKU,    # binary yes/no routing call
        "planning":  _SONNET,   # structured plan needs good reasoning
        "drafting":  _SONNET,   # core writing — don't compromise
        "editor":    _SONNET,   # structural review matters
        "polish":    _SONNET,   # final voice pass matters
        "critic":    _SONNET,   # gating; needs careful judgment
        "closing":   _HAIKU,    # 150-250 words of recovery prose
        "diagram":   _HAIKU,    # structured spec, not prose — Haiku is fine
        "diagram_review": _HAIKU,   # cheap semantic check
        "sources":   _HAIKU,    # short factual lookup — Haiku is fine
        "interviewer": _SONNET, # question + rubric quality drives the feature
        "evaluator":   _SONNET, # grading is judgment; inflated scores defeat it
        "resume_extract": _HAIKU,  # mechanical text→structure mapping
        "resume_review":  _SONNET, # critique quality drives the feature
        "resume_tailor":  _SONNET, # honesty guardrails need the strong model
    },
    "best": {
        "brief":     _SONNET,
        "relevance": _SONNET,
        "planning":  _SONNET,
        "drafting":  _SONNET,
        "editor":    _SONNET,
        "polish":    _SONNET,
        "critic":    _SONNET,
        "closing":   _SONNET,
        "diagram":   _SONNET,
        "diagram_review": _SONNET,   # cheap semantic check
        "sources":   _SONNET,
        "interviewer": _SONNET,
        "evaluator":   _SONNET,
        "resume_extract": _SONNET,
        "resume_review":  _SONNET,
        "resume_tailor":  _SONNET,
    },
    "fast": {
        "brief":     _HAIKU,    # acceptable quality loss for speed
        "relevance": _HAIKU,
        "planning":  _HAIKU,    # JSON-only output; Haiku handles this well
        "drafting":  _SONNET,   # never downgrade the core writer
        "editor":    _HAIKU,
        "polish":    _SONNET,   # final polish still needs Sonnet voice
        "critic":    _HAIKU,
        "closing":   _HAIKU,
        "diagram":   _HAIKU,
        "diagram_review": _HAIKU,   # cheap semantic check
        "sources":   _HAIKU,
        "interviewer": _HAIKU,  # structured output; acceptable loss for speed
        "evaluator":   _SONNET, # never downgrade the grader
        "resume_extract": _HAIKU,
        "resume_review":  _SONNET, # never downgrade the critic
        "resume_tailor":  _SONNET, # never downgrade the honesty-critical call
    },
}

_VALID_PRESETS = frozenset(_PRESETS)


def get_model(role: str, preset: str = "balanced") -> str:
    """Return the model string for *role* under *preset*.

    Unknown presets fall back to "balanced" so old cached ArticleRequests
    without a preset field still work after a code update.

    User model selection: presets decide which TIER a stage uses
    (strong vs light); the env overrides decide what model each tier IS —
    ANTHROPIC_STRONG_MODEL replaces every strong-tier (Sonnet) slot and
    ANTHROPIC_LIGHT_MODEL every light-tier (Haiku) slot. Read at call time
    so the settings UI's hot-reload takes effect without a restart. These
    also flow through the Claude CLI adapter (which passes unknown ids to
    `claude --model` verbatim), so one pair of knobs covers both paths.
    """
    if preset not in _PRESETS:
        preset = "balanced"
    table = _PRESETS[preset]
    if role not in table:
        raise ValueError(
            f"Unknown role {role!r}. Valid roles: {', '.join(sorted(ROLES))}"
        )
    model = table[role]
    if model == _SONNET:
        return os.environ.get("ANTHROPIC_STRONG_MODEL") or model
    if model == _HAIKU:
        return os.environ.get("ANTHROPIC_LIGHT_MODEL") or model
    return model
