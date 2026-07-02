"""Topic → official-documentation domain resolution.

The citation pipeline wants the *official docs* of whatever the article is
about to be the source of truth: kafka.apache.org for Kafka, docs.oracle.com
for Java, react.dev for React. A static domain list can never cover every
topic, so this worker asks a small model which domains are canonical for the
given topic, then validates and merges the answer with a static seed map.

The resolved domains feed two mechanisms downstream:
  1. A docs-first search pass restricted to those domains (so official pages
     are actually in the evidence pool).
  2. A trust-score boost in score_url() (so official pages win the fetch
     budget and the planner's evidence slots over blogs and Q&A forums).

Failure here must never sink the pipeline — on any error the resolver falls
back to static matches only, and an empty result simply means "no boost".
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel

from pipeline.model_config import get_model


# Fast-path seed map for very common technologies. Matched on word boundaries
# against the topic text; the LLM call covers everything else. Keep this
# small — it exists so the mock/no-LLM path and flaky-LLM runs still get
# doc-first behaviour for bread-and-butter topics.
_STATIC_SOURCES: dict[str, tuple[str, ...]] = {
    "kafka":      ("kafka.apache.org",),
    "java":       ("docs.oracle.com", "openjdk.org"),
    "python":     ("docs.python.org",),
    "javascript": ("developer.mozilla.org",),
    "typescript": ("typescriptlang.org",),
    "react":      ("react.dev",),
    "spring":     ("docs.spring.io",),
    "kubernetes": ("kubernetes.io",),
    "docker":     ("docs.docker.com",),
    "postgres":   ("postgresql.org",),
    "postgresql": ("postgresql.org",),
    "mysql":      ("dev.mysql.com",),
    "redis":      ("redis.io",),
    "mongodb":    ("mongodb.com",),
    "rust":       ("doc.rust-lang.org",),
    "golang":     ("go.dev",),
    "terraform":  ("developer.hashicorp.com",),
    "aws":        ("docs.aws.amazon.com",),
    "azure":      ("learn.microsoft.com",),
    "gcp":        ("cloud.google.com",),
}

# Bare hostname: letters/digits/hyphens/dots, at least one dot, sane TLD.
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

# Shared hosting / aggregator domains that must NEVER become "official docs".
# The LLM legitimately answers "github.com/<org>/<project>" for projects whose
# docs live in their repo (e.g. HikariCP) — but stripping the path would bless
# the ENTIRE host, and every random repo/post there would score 1.0 and crowd
# out real sources. These hosts keep their normal trust tier instead.
_SHARED_HOSTS = frozenset({
    "github.com", "gist.github.com", "raw.githubusercontent.com",
    "gitlab.com", "bitbucket.org", "sourceforge.net",
    "medium.com", "dev.to", "hashnode.dev", "substack.com",
    "stackoverflow.com", "stackexchange.com", "reddit.com", "quora.com",
    "wikipedia.org", "youtube.com", "npmjs.com", "pypi.org",
})

MAX_OFFICIAL_DOMAINS = 4

_SYSTEM_PROMPT = (
    "You identify the OFFICIAL documentation websites for software "
    "technologies. Given an article topic, return the bare domains of the "
    "canonical, first-party documentation for the technologies it involves — "
    "the vendor's or project's own docs (e.g. kafka.apache.org for Kafka, "
    "docs.oracle.com for Java, react.dev for React).\n"
    "Rules:\n"
    "- Bare domains only, no scheme, no path (docs.oracle.com — not "
    "https://docs.oracle.com/javase).\n"
    "- Only first-party sources. Never blogs, tutorial sites, Stack Overflow, "
    "Wikipedia, or aggregator sites.\n"
    "- Never shared hosting domains (github.com, gitlab.com, medium.com, "
    "npmjs.com, pypi.org). If a project's only documentation is its "
    "repository README, omit it rather than returning the hosting domain.\n"
    "- At most 4 domains, most relevant first.\n"
    "- If the topic involves no identifiable technology, return an empty list."
)


class OfficialSources(BaseModel):
    domains: list[str]


_SOURCES_TOOL: dict = {
    "name": "submit_official_sources",
    "description": "Submit the official documentation domains for the topic.",
    "input_schema": OfficialSources.model_json_schema(),
}


def _normalize_domain(raw: str) -> str | None:
    """Strip scheme/www/path from a candidate and validate it as a hostname.

    Rejects shared hosting/aggregator hosts: an "official" answer like
    github.com/<org>/<project> would otherwise bless all of github.com.
    """
    candidate = raw.strip().lower()
    candidate = re.sub(r"^[a-z][a-z0-9+.-]*://", "", candidate)  # scheme
    candidate = candidate.split("/", 1)[0].split("?", 1)[0]      # path/query
    candidate = candidate.removeprefix("www.")
    if not _DOMAIN_RE.match(candidate):
        return None
    if any(candidate == h or candidate.endswith(f".{h}") for h in _SHARED_HOSTS):
        return None
    return candidate


def static_official_sources(topic: str) -> list[str]:
    """Seed-map lookup: word-boundary match of known technology names."""
    words = set(re.findall(r"[a-z0-9+#.]+", topic.lower()))
    matched: list[str] = []
    for tech, domains in _STATIC_SOURCES.items():
        if tech in words:
            for domain in domains:
                if domain not in matched:
                    matched.append(domain)
    return matched


async def resolve_official_sources(
    topic: str,
    extra_context: str | None,
    client,
    preset: str = "balanced",
) -> list[str]:
    """Return up to MAX_OFFICIAL_DOMAINS official-doc domains for *topic*.

    Static seed matches come first (deterministic, never wrong for the common
    cases), then LLM-resolved domains fill the remaining slots. Any exception
    from the LLM degrades to static-only rather than failing the pipeline.
    """
    domains = static_official_sources(topic)

    user_content = f"topic: {topic}"
    if extra_context:
        user_content += f"\ncontext: {extra_context}"

    try:
        response = await client.messages.create(
            model=get_model("sources", preset),
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            tools=[_SOURCES_TOOL],
            tool_choice={"type": "tool", "name": "submit_official_sources"},
            messages=[{"role": "user", "content": user_content}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        resolved = OfficialSources.model_validate(tool_use.input)
        for raw in resolved.domains:
            domain = _normalize_domain(raw)
            if domain and domain not in domains:
                domains.append(domain)
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the run
        logging.warning(
            "Official-source resolution failed for topic %r (%s); "
            "using %d static match(es) only.",
            topic, exc, len(domains),
        )

    return domains[:MAX_OFFICIAL_DOMAINS]
