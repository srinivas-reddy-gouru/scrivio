"""Central model-selection configuration.

Three presets, one helper:
  balanced  (default) — Haiku for light routing tasks; Sonnet for writing.
  best                — Sonnet everywhere; maximum quality.
  fast                — Haiku wherever safe; Sonnet only for the core writer.

Workers call get_model(role, preset) instead of hard-coding a model string
so users can influence cost vs. quality from the UI without touching code.
"""
from __future__ import annotations

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
    "diagram",      # Mermaid spec generation — structured, not prose
    "sources",      # topic → official-docs domain resolution (tiny JSON call)
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
        "sources":   _HAIKU,    # short factual lookup — Haiku is fine
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
        "sources":   _SONNET,
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
        "sources":   _HAIKU,
    },
}

_VALID_PRESETS = frozenset(_PRESETS)


def get_model(role: str, preset: str = "balanced") -> str:
    """Return the model string for *role* under *preset*.

    Unknown presets fall back to "balanced" so old cached ArticleRequests
    without a preset field still work after a code update.
    """
    if preset not in _PRESETS:
        preset = "balanced"
    table = _PRESETS[preset]
    if role not in table:
        raise ValueError(
            f"Unknown role {role!r}. Valid roles: {', '.join(sorted(ROLES))}"
        )
    return table[role]
