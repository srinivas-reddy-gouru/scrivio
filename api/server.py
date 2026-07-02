import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ModuleNotFoundError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.jobs import Job, create_job, get_job
from main import _anthropic_client, generate_article
from pipeline.schemas.models import (
    ArticleRequest,
    ClarificationQuestion,
    ClarificationQuestions,
    ProgressEvent,
)
from pipeline.workers.clarification_questions_worker import (
    generate_clarification_questions,
)
from pipeline.workers.topic_classifier import classify_topic_breadth


# Where finished articles are written to disk. Each job creates a
# timestamped subdirectory containing one markdown file per explanation
# level plus a meta.json with the request and verification reports.
OUTPUT_ROOT = Path(os.environ.get("ARTICLE_OUTPUT_DIR", "./output"))


app = FastAPI(title="Article Generator API", version="0.1.0")

# Permissive for local dev; tighten before any public deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateResponse(BaseModel):
    """Two-shaped response: either a job started, or clarification is needed.

    Frontends inspect `clarification_required` first. When false (or absent
    by being default), `job_id` is set and the SSE stream can be opened.
    When true, no job is running — the frontend collects answers and reposts
    to /generate with `clarification_answers` filled in.
    """
    job_id: str | None = None
    clarification_required: bool = False
    questions: list[ClarificationQuestion] = []
    default_if_skipped: str = ""


class JobStatusResponse(BaseModel):
    status: str
    error: str | None = None
    articles: dict | None = None


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


def _is_steered(request: ArticleRequest) -> bool:
    """Has the user given the system enough direction to skip clarification?

    Any one of these counts as steering:
    - `extra_context` non-empty (free-form direction)
    - `must_cover` non-empty (explicit sub-topic list)
    - `clarification_answers` non-empty (this is a *resubmission* with answers)
    - `skip_clarification=True` (the client explicitly opted out)
    """
    return bool(
        request.extra_context
        or request.must_cover
        or request.clarification_answers
        or request.skip_clarification
    )


def _apply_clarification_answers(request: ArticleRequest) -> ArticleRequest:
    """Compose any clarification_answers into extra_context so downstream
    agents (brief, planner, drafter, editor) all see the same merged steering.

    We preserve the original extra_context if any, then append a compact
    formatted block of the answers. We also pull a `must_cover` value out of
    the answers (if present) and merge it into the request's must_cover list.
    """
    if not request.clarification_answers:
        return request

    answers = dict(request.clarification_answers)

    # The free-text "anything specific to cover?" answer is treated as
    # additional must_cover entries when phrased as a list, or appended to
    # extra_context as-is otherwise.
    must_cover_text = answers.pop("must_cover", "").strip()
    new_must_cover = list(request.must_cover)
    if must_cover_text:
        # Split on common separators. Single-item answers ("indexing strategies")
        # become a one-element list; comma-separated answers expand.
        for piece in [p.strip() for p in must_cover_text.replace(";", ",").split(",")]:
            if piece and piece not in new_must_cover:
                new_must_cover.append(piece)

    formatted = "; ".join(f"{k}: {v}" for k, v in answers.items() if v)
    parts = []
    if request.extra_context:
        parts.append(request.extra_context)
    if formatted:
        parts.append(f"clarification_answers: {formatted}")
    if new_must_cover:
        parts.append(f"must_cover: {', '.join(new_must_cover)}")

    return request.model_copy(
        update={
            "extra_context": " | ".join(parts),
            "must_cover": new_must_cover,
        }
    )


async def _maybe_request_clarification(
    request: ArticleRequest,
) -> ClarificationQuestions | None:
    """If the request is broad and unsteered, classify and (if broad) generate
    clarification questions. Returns None when the request is steered or
    classified narrow — in that case the caller should start the job."""
    if _is_steered(request):
        return None

    client = _anthropic_client(request)
    breadth = await classify_topic_breadth(
        request.topic, request.extra_context, client
    )
    if breadth == "narrow":
        return None

    return await generate_clarification_questions(request.topic, breadth, client)


@app.post("/generate", response_model=GenerateResponse)
async def generate(request: ArticleRequest) -> GenerateResponse:
    # Step 1: if the topic is broad and the user hasn't given us steering,
    # ask clarifying questions instead of starting a job.
    clarification = await _maybe_request_clarification(request)
    if clarification is not None:
        return GenerateResponse(
            clarification_required=True,
            questions=clarification.questions,
            default_if_skipped=clarification.default_if_skipped,
        )

    # Step 2: merge clarification_answers (if any) into extra_context so
    # every downstream agent sees the user's steering.
    effective_request = _apply_clarification_answers(request)

    # Step 3: start the job exactly as before.
    job = create_job()

    async def callback(event: ProgressEvent) -> None:
        await job.publish(event)

    # Keep a handle on the asyncio.Task so the cancel endpoint can stop it.
    # Without this the running pipeline can't be interrupted — closing the
    # SSE stream from the client side wouldn't help.
    job.task = asyncio.create_task(_run_job(job, effective_request, callback))
    return GenerateResponse(job_id=job.job_id)


@app.post("/clarify", response_model=ClarificationQuestions)
async def clarify(request: ArticleRequest) -> ClarificationQuestions:
    """Return clarification questions for a topic without starting a job.

    Useful for UIs that want to surface the questions before the user even
    sees a "Generate" button — or for any client that wants to inspect the
    questions independently of the /generate decision logic.
    """
    client = _anthropic_client(request)
    breadth = await classify_topic_breadth(
        request.topic, request.extra_context, client
    )
    if breadth == "narrow":
        # Even for narrow topics we return a minimal "no questions" payload
        # rather than 4xx — keeps the frontend's caller logic simple.
        return ClarificationQuestions(
            questions=[],
            default_if_skipped=(
                f"The topic '{request.topic}' is specific enough to generate without "
                "clarification."
            ),
        )
    return await generate_clarification_questions(request.topic, breadth, client)


async def _run_job(
    job: Job, request: ArticleRequest, callback
) -> None:
    try:
        result = await generate_article(request, progress_callback=callback)
        job.result = result

        # Persist to disk so the article survives a server restart and so
        # the user has a real file to open rather than scrolling SSE output.
        output_dir = _persist_job(job.job_id, request, result)
        logging.info("Job %s articles saved to %s", job.job_id, output_dir)

        await job.publish(
            ProgressEvent(
                type="complete",
                stage="complete",
                data={
                    "output_dir": str(output_dir),
                    "articles": {
                        level: article.model_dump(mode="json")
                        for level, article in result.items()
                    },
                },
            )
        )
    except asyncio.CancelledError:
        # Explicit cancellation via the /jobs/{id} DELETE endpoint. We
        # publish a terminal `cancelled` event so the SSE client knows to
        # stop and update the UI accordingly. Don't re-raise — the task
        # has done its cleanup and ending here is the intended outcome.
        logging.info("Job %s cancelled by user", job.job_id)
        job.error = "Cancelled by user"
        await job.publish(
            ProgressEvent(
                type="cancelled", stage="cancelled",
                message="Cancelled by user",
            )
        )
    except Exception as exc:
        logging.exception("Job %s failed", job.job_id)
        job.error = str(exc)
        await job.publish(
            ProgressEvent(type="error", stage="error", message=str(exc))
        )
    finally:
        await job.close()


@app.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict:
    """Cancel an in-flight job. Returns 404 if the job doesn't exist,
    200 with `{"cancelled": false}` if it already finished, and 200 with
    `{"cancelled": true}` if cancellation was actually signaled."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    was_running = job.cancel()
    return {"cancelled": was_running, "job_id": job_id}


def _persist_job(
    job_id: str,
    request: ArticleRequest,
    result: dict,
) -> Path:
    """Write the generated articles + metadata to disk under OUTPUT_ROOT.

    Layout:
        output/<timestamp>__<slug>__<short-id>/
            meta.json
            basic.md
            intermediate.md
            advanced.md
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _slug(request.topic)[:60]
    short_id = job_id[:8]
    job_dir = OUTPUT_ROOT / f"{timestamp}__{slug}__{short_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Write one markdown file per explanation level.
    for level, article in result.items():
        (job_dir / f"{level}.md").write_text(article.markdown, encoding="utf-8")

    # Write a meta.json with the request and the verification reports
    # (sources, claim support status, etc.) — useful for traceability.
    first_article = next(iter(result.values()))
    meta = {
        "job_id": job_id,
        "generated_at": datetime.now().isoformat(),
        "request": request.model_dump(mode="json"),
        "verification_reports": [
            r.model_dump(mode="json") for r in first_article.verification_reports
        ],
        "assets": [a.model_dump(mode="json") for a in first_article.assets],
    }
    (job_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, default=str), encoding="utf-8"
    )
    return job_dir


def _slug(text: str) -> str:
    """Filesystem-safe slug for a topic string."""
    slug = "".join(c.lower() if c.isalnum() else "-" for c in text)
    return "-".join(part for part in slug.split("-") if part) or "article"


@app.get("/jobs/{job_id}/stream")
async def stream(job_id: str) -> StreamingResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_source() -> AsyncGenerator[str, None]:
        while True:
            event = await job.queue.get()
            if event is None:
                break
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if reverse-proxied
        },
    )


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.cancelled:
        return JobStatusResponse(status="cancelled", error=job.error or "Cancelled by user")
    if job.error:
        return JobStatusResponse(status="error", error=job.error)
    if job.result is not None:
        articles = {
            level: article.model_dump(mode="json")
            for level, article in job.result.items()
        }
        return JobStatusResponse(status="complete", articles=articles)
    return JobStatusResponse(status="pending")


# ── Article history ──────────────────────────────────────────────────
# Read articles back out of the output directory. The disk is the source
# of truth — in-memory jobs disappear on server restart, but the saved
# articles persist as long as their directory does.

# Only directory names matching this pattern are served from /articles/{id}.
# Prevents path-traversal: a request like /articles/..%2Fetc%2Fpasswd
# fails the regex and 404s before any filesystem access.
_ARTICLE_DIR_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


class ArticleVersion(BaseModel):
    """One run within a re-run lineage."""
    id: str
    version: int          # 1 = original, 2 = first re-run, …
    generated_at: str


class ArticleSummary(BaseModel):
    id: str  # directory name
    title: str
    topic: str
    level: str  # the level that was generated
    generated_at: str
    available_levels: list[str]  # which markdown files exist on disk
    rerun_of: str | None = None
    version: int = 1
    # Full lineage (oldest → newest), populated on the representative entry
    # the library shows. A single-run article has just itself here.
    versions: list[ArticleVersion] = []


class ArticleDetail(BaseModel):
    id: str
    title: str
    topic: str
    level: str
    generated_at: str
    available_levels: list[str]
    markdown: str
    # The original ArticleRequest — lets the UI prefill the composer for
    # "Re-run" with every knob the article was generated with.
    request: dict = {}
    version: int = 1
    versions: list[ArticleVersion] = []


def _read_article_meta(article_dir: Path) -> dict | None:
    """Read meta.json. Returns None for directories that aren't valid
    article output (no meta, malformed JSON, missing required fields)."""
    meta_path = article_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _summary_from_meta(article_dir: Path, meta: dict) -> ArticleSummary | None:
    """Convert a meta dict + directory listing into an ArticleSummary.
    Returns None if the directory has no markdown content."""
    request = meta.get("request") or {}
    title = (
        meta.get("title")
        or request.get("topic")
        or article_dir.name
    )
    # Discover which levels were generated by checking for .md files.
    available_levels = sorted(
        p.stem for p in article_dir.iterdir()
        if p.is_file() and p.suffix == ".md"
    )
    if not available_levels:
        return None
    requested_level = request.get("explanation_level") or available_levels[0]
    # If the requested level isn't on disk, fall back to whatever is.
    if requested_level not in available_levels:
        requested_level = available_levels[0]
    return ArticleSummary(
        id=article_dir.name,
        title=title,
        topic=request.get("topic", ""),
        level=requested_level,
        generated_at=meta.get("generated_at", ""),
        available_levels=available_levels,
        rerun_of=request.get("rerun_of"),
    )


def _scan_summaries() -> list[tuple[ArticleSummary, float]]:
    """All valid article summaries on disk as (summary, dir mtime) pairs."""
    if not OUTPUT_ROOT.exists():
        return []
    pairs: list[tuple[ArticleSummary, float]] = []
    for article_dir in OUTPUT_ROOT.iterdir():
        if not article_dir.is_dir():
            continue
        meta = _read_article_meta(article_dir)
        if meta is None:
            continue
        summary = _summary_from_meta(article_dir, meta)
        if summary is not None:
            pairs.append((summary, article_dir.stat().st_mtime))
    return pairs


def _group_into_lineages(
    pairs: list[tuple[ArticleSummary, float]]
) -> list[ArticleSummary]:
    """Collapse re-run chains into one representative summary per lineage.

    Each article's root is found by walking rerun_of pointers (broken or
    cyclic pointers degrade to "this article is its own root" — a deleted
    ancestor must not hide its descendants). Runs are numbered oldest = 1,
    and the NEWEST run represents the lineage in the library, carrying the
    full version list so the UI can offer older runs. Directory mtime is
    the ordering key (matching the pre-lineage history behaviour); the
    generated_at string is display metadata only.
    """
    by_id = {s.id: s for s, _ in pairs}

    def root_of(summary: ArticleSummary) -> str:
        seen = set()
        current = summary
        while current.rerun_of and current.rerun_of in by_id and current.id not in seen:
            seen.add(current.id)
            current = by_id[current.rerun_of]
        return current.id

    lineages: dict[str, list[tuple[ArticleSummary, float]]] = {}
    for s, mtime in pairs:
        lineages.setdefault(root_of(s), []).append((s, mtime))

    representatives: list[tuple[ArticleSummary, float]] = []
    for runs in lineages.values():
        runs.sort(key=lambda pair: pair[1])  # oldest first
        versions = [
            ArticleVersion(id=r.id, version=i + 1, generated_at=r.generated_at)
            for i, (r, _) in enumerate(runs)
        ]
        latest, latest_mtime = runs[-1]
        representatives.append((
            latest.model_copy(update={"version": len(runs), "versions": versions}),
            latest_mtime,
        ))

    representatives.sort(key=lambda pair: pair[1], reverse=True)
    return [s for s, _ in representatives]


@app.get("/articles", response_model=list[ArticleSummary])
async def list_articles(limit: int = 50) -> list[ArticleSummary]:
    """List previously-generated articles, newest first.

    `limit` caps the response size so a directory with hundreds of
    articles doesn't blow up the UI. The default of 50 fits comfortably
    in the history sidebar.
    """
    # Scan everything, THEN group and cap: a lineage member older than the
    # limit window must still fold into its representative card.
    return _group_into_lineages(_scan_summaries())[:limit]


@app.get("/articles/{article_id}", response_model=ArticleDetail)
async def get_article(article_id: str, level: str | None = None) -> ArticleDetail:
    """Return one article's markdown plus its metadata.

    `level` lets the client request a specific level when the article
    has more than one on disk; defaults to whichever level was the
    user's original request, falling back to the first available file.
    """
    if not _ARTICLE_DIR_PATTERN.match(article_id):
        # Path-traversal attempt or malformed id.
        raise HTTPException(status_code=404, detail="Article not found")

    article_dir = OUTPUT_ROOT / article_id
    if not article_dir.is_dir():
        raise HTTPException(status_code=404, detail="Article not found")

    meta = _read_article_meta(article_dir)
    if meta is None:
        raise HTTPException(status_code=404, detail="Article metadata missing")

    summary = _summary_from_meta(article_dir, meta)
    if summary is None:
        raise HTTPException(status_code=404, detail="Article has no markdown")

    target_level = level or summary.level
    if target_level not in summary.available_levels:
        raise HTTPException(
            status_code=404,
            detail=f"Level '{target_level}' not generated for this article. "
                   f"Available: {summary.available_levels}",
        )
    markdown = (article_dir / f"{target_level}.md").read_text(encoding="utf-8")

    # Lineage lookup so the article view can offer a version dropdown and
    # the Re-run action knows this article's place in its chain.
    version, versions = 1, []
    for representative in _group_into_lineages(_scan_summaries()):
        for v in representative.versions:
            if v.id == summary.id:
                version, versions = v.version, representative.versions
                break
        if versions:
            break

    return ArticleDetail(
        id=summary.id,
        title=summary.title,
        topic=summary.topic,
        level=target_level,
        generated_at=summary.generated_at,
        available_levels=summary.available_levels,
        markdown=markdown,
        request=meta.get("request") or {},
        version=version,
        versions=versions,
    )


# ── Settings endpoints ────────────────────────────────────────────────
# Read / write API keys and toggles in the project-root .env file.
# Keys are never returned in plaintext — only masked (last 4 chars shown).

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# API keys the settings UI shows/edits, in display order.
# LLM_PROVIDER is NOT here — it's a preference value, not a secret key.
# It is read/written via the dedicated provider_preference field.
_MANAGED_KEYS: list[tuple[str, str]] = [
    ("ANTHROPIC_API_KEY", "Anthropic — Claude models for the writing pipeline (brief, plan, draft, edit, polish, critic)"),
    ("OPENAI_API_KEY",    "OpenAI — GPT models; runs the full pipeline when it is the only key, plus search queries and claim verification"),
    ("TAVILY_API_KEY",    "Tavily — enables live web search for citations (optional)"),
    ("JINA_API_KEY",      "Jina Reader — fallback fetcher for pages that block scrapers (optional; Jina returns 401 without it)"),
    ("USE_JINA_READER",   "Try Jina Reader FIRST for URL extraction (true / false)"),
]

# Search-provider env vars (any one is sufficient).
_SEARCH_KEYS = ("TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY", "EXA_API_KEY")


def _read_env_file() -> dict[str, str]:
    """Parse the .env file into a plain dict (key → raw value, no quoting)."""
    result: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return result
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip()
    return result


def _write_env_file(pairs: dict[str, str]) -> None:
    """Write *pairs* to the .env file, preserving unmanaged lines."""
    existing_lines: list[str] = []
    if _ENV_FILE.exists():
        existing_lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()

    # Collect keys we'll manage (update in-place or append).
    managed_set = {k for k, _ in _MANAGED_KEYS} | set(pairs.keys())
    output_lines: list[str] = []
    updated_keys: set[str] = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in pairs:
            output_lines.append(f"{k}={pairs[k]}")
            updated_keys.add(k)
        else:
            output_lines.append(line)

    # Append any new keys not already in the file.
    for k, v in pairs.items():
        if k not in updated_keys:
            output_lines.append(f"{k}={v}")

    _ENV_FILE.write_text("\n".join(output_lines) + "\n", encoding="utf-8")


def _mask_value(v: str) -> str:
    """Return last-4-chars mask, e.g. '••••••••xxxx', or empty string."""
    if not v:
        return ""
    visible = v[-4:] if len(v) > 4 else v
    return "••••" + visible


class KeyStatus(BaseModel):
    key: str
    description: str
    present: bool
    masked_value: str  # last 4 chars only; empty when key is absent


class SettingsResponse(BaseModel):
    keys: list[KeyStatus]
    # ── Resolved state (computed server-side so the UI never has to guess) ──
    resolved_provider: str   # "anthropic" | "openai" | "none"
    provider_auto: bool      # True = single key present (auto-selected)
                             # False = both keys present, preference applied
    provider_preference: str # "anthropic" | "openai" — only meaningful when both keys present
    has_search: bool         # True when any search key is configured
    has_anthropic: bool
    has_openai: bool


class SettingsPatch(BaseModel):
    updates: dict[str, str]  # key_name → new_value (empty string = remove)
                             # LLM_PROVIDER is accepted here as a preference value


def _resolved_state(env: dict) -> dict:
    """Compute provider resolution using the same logic as main._resolve_provider()."""
    def _present(key: str) -> bool:
        return bool(env.get(key) or os.environ.get(key))

    has_anthropic = _present("ANTHROPIC_API_KEY")
    has_openai    = _present("OPENAI_API_KEY")
    has_search    = any(_present(k) for k in _SEARCH_KEYS)
    pref_raw      = env.get("LLM_PROVIDER") or os.environ.get("LLM_PROVIDER", "anthropic")
    preference    = pref_raw.strip().lower() if pref_raw else "anthropic"
    if preference not in ("anthropic", "openai"):
        preference = "anthropic"

    if has_openai and not has_anthropic:
        resolved, auto = "openai", True
    elif has_anthropic and not has_openai:
        resolved, auto = "anthropic", True
    elif has_anthropic and has_openai:
        resolved, auto = preference, False
    else:
        resolved, auto = "none", True

    return {
        "resolved_provider": resolved,
        "provider_auto": auto,
        "provider_preference": preference,
        "has_search": has_search,
        "has_anthropic": has_anthropic,
        "has_openai": has_openai,
    }


@app.get("/settings", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    """Return key status plus the resolved LLM provider and search availability."""
    env = _read_env_file()
    keys = []
    for key_name, description in _MANAGED_KEYS:
        raw = env.get(key_name, "") or os.environ.get(key_name, "")
        keys.append(KeyStatus(
            key=key_name,
            description=description,
            present=bool(raw),
            masked_value=_mask_value(raw),
        ))
    return SettingsResponse(keys=keys, **_resolved_state(env))


@app.patch("/settings")
async def update_settings(body: SettingsPatch) -> dict:
    """Write updated key values to the .env file and reload into os.environ.

    Pass an empty string for a key to clear it.
    Only keys listed in _MANAGED_KEYS may be updated.
    """
    # LLM_PROVIDER is a preference value (not a secret key) — allow it here
    # even though it is not listed in _MANAGED_KEYS.
    allowed = {k for k, _ in _MANAGED_KEYS} | {"LLM_PROVIDER"}
    rejected = [k for k in body.updates if k not in allowed]
    if rejected:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown keys: {rejected}. Allowed: {sorted(allowed)}",
        )

    updates = {k: v for k, v in body.updates.items() if v}
    clears = {k for k, v in body.updates.items() if not v}

    # Read current file, strip cleared keys, merge updates.
    current = _read_env_file()
    for k in clears:
        current.pop(k, None)
    current.update(updates)
    _write_env_file(current)

    # Hot-reload into the running process so the change takes effect
    # without a server restart.
    for k, v in updates.items():
        os.environ[k] = v
    for k in clears:
        os.environ.pop(k, None)

    return {"ok": True, "updated": list(updates), "cleared": list(clears)}


# ── Static UI ────────────────────────────────────────────────────────
# Serves ui/index.html at GET / and any other files (e.g. ui/style.css)
# directly. Registered LAST so all the /generate, /jobs/*, /clarify
# route handlers above take precedence over the static file lookup.
# `html=True` makes the mount serve index.html for the root path.
_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
