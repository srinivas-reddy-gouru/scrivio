import asyncio
import base64
import binascii
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

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from uuid import uuid4

from api.jobs import Job, create_job, get_job
from main import _anthropic_client, generate_article
from pipeline.schemas.models import (
    AnswerEvaluation,
    ArticleRequest,
    ClarificationQuestion,
    ClarificationQuestions,
    InterviewAnswerRecord,
    InterviewMode,
    InterviewQuestionState,
    InterviewQuestionStatus,
    InterviewSession,
    InterviewSummary,
    JobAnalysis,
    JobProfile,
    ProgressEvent,
    ResumeChange,
    ResumeDoc,
    StructuredResume,
    TailoredResume,
)
from pipeline.workers.answer_evaluator_worker import (
    evaluate_answer,
    generate_debrief,
    interview_memory_digest,
)
from pipeline.workers.job_interviewer_worker import (
    analyze_job_fit,
    competency_for_question,
    generate_job_interview,
    generate_job_scorecard,
    research_job_questions,
)
from pipeline.workers.resume_parser import ResumeParseError, parse_resume
from pipeline.workers.resume_studio_worker import (
    advise_resume,
    apply_tailored_edits,
    build_resume_advice_context,
    edit_resume_by_instruction,
    extract_resume,
    guard_edited_numbers_and_log,
    fill_metric_placeholders,
    from_jsonresume,
    render_docx,
    render_markdown,
    render_pdf,
    review_resume,
    run_ats_checks,
    tailor_resume,
    to_jsonresume,
)
from pipeline.workers.interviewer_worker import (
    find_real_question_patterns,
    generate_interview_questions,
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
    # ── Model selection ─────────────────────────────────────────────
    # The app describes the TASK; the user picks the model. Two task sizes:
    # LARGE = writing, editing, interview grading (quality-sensitive).
    # SMALL = routing, relevance checks, diagrams, source lookups (high
    # volume, quality-tolerant). Any model may be assigned to either —
    # the choice and its trade-offs belong to the user.
    ("ANTHROPIC_STRONG_MODEL", "Article writing, editing, interview grading. Pick any Claude model; your call entirely."),
    ("ANTHROPIC_LIGHT_MODEL",  "Routing, relevance checks, diagrams, source lookups. High call volume."),
    ("OPENAI_STRONG_MODEL",    "Article writing, editing, interview grading. Pick any OpenAI model."),
    ("OPENAI_LIGHT_MODEL",     "Routing, checks, diagrams. High call volume."),
    ("CLAUDE_CLI_MODEL",       "Every subscription call uses this one model, ignoring the large/small split. Leave on default to keep the split."),
    # ── Local CLI provider selection ────────────────────────────────
    ("LLM_CLI",           "Which local AI CLI runs the subscription provider: claude (Claude Pro/Max), codex (ChatGPT), gemini (Google), qwen, or ollama (local, no account)."),
    ("CLI_STRONG_MODEL",  "Non-Claude CLIs: model for LARGE tasks. Empty = the CLI's default strong model."),
    ("CLI_LIGHT_MODEL",   "Non-Claude CLIs: model for SMALL tasks. Empty = the CLI's default light model."),
    ("CLI_FORCE_MODEL",   "Any CLI: force EVERY call onto this one model, beating all other choices."),
]

# Displayed and edited in plain text — these are preferences, not secrets.
_PLAIN_KEYS = {
    "USE_JINA_READER",
    "ANTHROPIC_STRONG_MODEL", "ANTHROPIC_LIGHT_MODEL",
    "OPENAI_STRONG_MODEL", "OPENAI_LIGHT_MODEL", "CLAUDE_CLI_MODEL",
    "LLM_CLI", "CLI_STRONG_MODEL", "CLI_LIGHT_MODEL", "CLI_FORCE_MODEL",
}

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

    # Collect keys we'll manage (update in-place, append, or drop-if-cleared).
    managed_set = {k for k, _ in _MANAGED_KEYS} | {"LLM_PROVIDER"} | set(pairs.keys())
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
        elif k in managed_set:
            # Managed key absent from pairs = cleared — drop the line.
            # (Previously this branch was missing, so "clear" removed the
            # value from os.environ but left the stale line in .env, and
            # the old value came back on the next restart.)
            continue
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
    masked_value: str  # secrets: last 4 chars; plain keys: full value
    plain: bool = False  # True = not a secret; UI shows/edits it in clear text


class SettingsResponse(BaseModel):
    keys: list[KeyStatus]
    # ── Resolved state (computed server-side so the UI never has to guess) ──
    resolved_provider: str   # "anthropic" | "openai" | "claude-cli" | "none"
    provider_auto: bool      # True = single key present (auto-selected)
                             # False = both keys present, preference applied
    provider_preference: str # "anthropic" | "openai" | "claude-cli"
    has_search: bool         # True when any search key is configured
    has_anthropic: bool
    has_openai: bool
    # BYO subscription: a local AI CLI is installed, so LLM calls can run
    # on the user's existing login with no API key. Which CLI is active is
    # configuration (LLM_CLI); detected_clis lists every one installed.
    has_claude_cli: bool = False
    active_cli: str = "claude"
    detected_clis: list[str] = []


class SettingsPatch(BaseModel):
    updates: dict[str, str]  # key_name → new_value (empty string = remove)
                             # LLM_PROVIDER is accepted here as a preference value


def _resolved_state(env: dict) -> dict:
    """Compute provider resolution using the same logic as main._resolve_provider()."""
    from pipeline.providers.claude_cli_adapter import (
        active_cli_name,
        claude_cli_available,
        detected_clis,
    )

    def _present(key: str) -> bool:
        return bool(env.get(key) or os.environ.get(key))

    has_anthropic  = _present("ANTHROPIC_API_KEY")
    has_openai     = _present("OPENAI_API_KEY")
    has_claude_cli = claude_cli_available()
    # A search key OR the CLI's WebSearch tool (subscription) enables search.
    has_search     = any(_present(k) for k in _SEARCH_KEYS) or has_claude_cli
    pref_raw       = env.get("LLM_PROVIDER") or os.environ.get("LLM_PROVIDER", "anthropic")
    preference     = pref_raw.strip().lower() if pref_raw else "anthropic"
    if preference not in ("anthropic", "openai", "claude-cli"):
        preference = "anthropic"

    if preference == "claude-cli" and has_claude_cli:
        # Explicit subscription preference beats API keys (mirrors
        # main._resolve_provider — the whole point is not spending them).
        resolved, auto = "claude-cli", False
    elif has_openai and not has_anthropic:
        resolved, auto = "openai", True
    elif has_anthropic and not has_openai:
        resolved, auto = "anthropic", True
    elif has_anthropic and has_openai:
        resolved, auto = preference if preference != "claude-cli" else "anthropic", False
    elif has_claude_cli:
        resolved, auto = "claude-cli", True
    else:
        resolved, auto = "none", True

    return {
        "resolved_provider": resolved,
        "provider_auto": auto,
        "provider_preference": preference,
        "has_search": has_search,
        "has_anthropic": has_anthropic,
        "has_openai": has_openai,
        "has_claude_cli": has_claude_cli,
        "active_cli": active_cli_name(),
        "detected_clis": detected_clis(),
    }


@app.get("/settings", response_model=SettingsResponse)
async def get_settings() -> SettingsResponse:
    """Return key status plus the resolved LLM provider and search availability."""
    env = _read_env_file()
    keys = []
    for key_name, description in _MANAGED_KEYS:
        raw = env.get(key_name, "") or os.environ.get(key_name, "")
        plain = key_name in _PLAIN_KEYS
        keys.append(KeyStatus(
            key=key_name,
            description=description,
            present=bool(raw),
            masked_value=raw if plain else _mask_value(raw),
            plain=plain,
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


# ── Interview practice ───────────────────────────────────────────────
# Free-text interview questions with hidden rubric + model answer, graded
# strictly against that rubric (anti-sycophancy: the bar is written before
# the user answers). Sessions persist as one JSON file each so history and
# resume survive server restarts. Two entry modes: topic-only (cheap, no
# article needed) and article-attached (grounded in the article's content
# and verified claims).

_INTERVIEWS_DIR = "interviews"  # subdirectory of OUTPUT_ROOT

# Same anti-traversal approach as _ARTICLE_DIR_PATTERN: ids that don't match
# 404 before any filesystem access.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9\-]+$")

# Serializes read-modify-write per session so a double-submit can't corrupt
# the session file. In-process only — matches the single-process deployment.
_interview_locks: dict[str, asyncio.Lock] = {}

_VALID_LEVELS = ("basic", "intermediate", "advanced")


def _session_lock(session_id: str) -> asyncio.Lock:
    return _interview_locks.setdefault(session_id, asyncio.Lock())


def _sessions_root() -> Path:
    return OUTPUT_ROOT / _INTERVIEWS_DIR


def _session_path(session_id: str) -> Path:
    return _sessions_root() / f"{session_id}.json"


def _load_session(session_id: str) -> InterviewSession:
    if not _SESSION_ID_PATTERN.match(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    path = _session_path(session_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        return InterviewSession.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except Exception:
        # Torn/corrupt file: 410 tells the client the id was real but the
        # data is gone, distinct from a plain 404.
        raise HTTPException(status_code=410, detail="Session file unreadable")


def _save_session(session: InterviewSession) -> None:
    """Atomic write: crash mid-write leaves the previous file intact."""
    root = _sessions_root()
    root.mkdir(parents=True, exist_ok=True)
    path = _session_path(session.session_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(session.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _final_evaluation(state: InterviewQuestionState) -> AnswerEvaluation | None:
    """The evaluation that counts: the combined follow-up one when a
    follow-up round happened, else the first."""
    if state.followup is not None:
        return state.followup.evaluation
    if state.first is not None:
        return state.first.evaluation
    return None


def _compute_summary(session: InterviewSession) -> InterviewSummary:
    """Deterministic aggregate — no LLM call (the simulation debrief is
    added separately and is failure-tolerant). The per-question evaluations
    are already structured; an LLM summary would add cost, latency, and a
    fresh sycophancy surface for zero information gain."""
    answered = [q for q in session.questions if q.status == "completed"]
    skipped = [q for q in session.questions if q.status == "skipped"]
    scores = [q.final_score for q in answered if q.final_score is not None]

    # Confidence calibration: mean |predicted - actual| where both exist.
    deltas = [
        abs(q.first.predicted_score - q.final_score)
        for q in answered
        if q.first is not None
        and q.first.predicted_score is not None
        and q.final_score is not None
    ]

    weak_counts: dict[str, int] = {}
    top_gaps: list[str] = []
    for state in answered:
        if state.final_score is None or state.final_score > 6:
            continue
        evaluation = _final_evaluation(state)
        if evaluation is None:
            continue
        for pointer in evaluation.section_pointers:
            weak_counts[pointer] = weak_counts.get(pointer, 0) + 1
        top_gaps.extend(evaluation.gaps)

    return InterviewSummary(
        total_questions=len(session.questions),
        answered=len(answered),
        skipped=len(skipped),
        average_score=round(sum(scores) / len(scores), 1) if scores else None,
        per_question=[
            {
                "id": q.question.id,
                "question": q.question.question,
                "final_score": q.final_score,
                "status": q.status,
            }
            for q in session.questions
        ],
        weak_sections=sorted(weak_counts, key=weak_counts.get, reverse=True),
        top_gaps=top_gaps[:5],
        calibration_gap=(
            round(sum(deltas) / len(deltas), 1) if deltas else None
        ),
    )


# ── Redaction: what the client is allowed to see ─────────────────────
# The rubric and model answer exist on disk from the moment a session is
# created, but the client only receives them once the question is terminal
# (completed or skipped). While a question is open they would let the user
# game the grader — and leak the bar the evaluator is scoring against.

class InterviewQuestionPublic(BaseModel):
    # model_answer is ours, not Pydantic's — silence the namespace warning.
    model_config = ConfigDict(protected_namespaces=())

    id: str
    question: str
    difficulty: str
    section_anchor: str
    status: InterviewQuestionStatus
    first_answer: str | None = None
    first_evaluation: AnswerEvaluation | None = None
    followup_question: str | None = None
    followup_answer: str | None = None
    final_evaluation: AnswerEvaluation | None = None
    model_answer: str | None = None
    rubric_key_points: list[str] | None = None
    final_score: int | None = None
    predicted_score: int | None = None


class InterviewSessionPublic(BaseModel):
    session_id: str
    article_id: str | None
    topic: str
    level: str
    mode: str = "practice"
    job_profile_id: str | None = None
    duration_minutes: int = 45
    created_at: str
    updated_at: str
    complete: bool
    questions: list[InterviewQuestionPublic]
    summary: InterviewSummary | None


class InterviewSessionSummaryItem(BaseModel):
    session_id: str
    article_id: str | None
    topic: str
    level: str
    mode: str = "practice"
    job_profile_id: str | None = None
    created_at: str
    complete: bool
    answered: int
    total: int
    average_score: float | None


def _public_question(
    state: InterviewQuestionState,
    mode: str = "practice",
    session_complete: bool = False,
) -> InterviewQuestionPublic:
    q = state.question
    public = InterviewQuestionPublic(
        id=q.id,
        question=q.question,
        difficulty=q.difficulty,
        section_anchor=q.section_anchor,
        status=state.status,
    )
    # Simulation/job redaction: mid-session, a completed question reveals
    # NOTHING beyond its status — the whole point is no feedback until the
    # end of the screen. Everything unlocks once the session is complete.
    # Job mode differs in one respect: follow-ups exist (real interviewers
    # probe), so an open follow-up question must be visible — but the
    # interim evaluation behind it stays hidden.
    if mode in ("simulation", "job") and not session_complete:
        if (
            mode == "job"
            and state.status == "awaiting_followup"
            and state.first is not None
        ):
            public.followup_question = state.first.evaluation.followup_question
        return public
    if state.status == "awaiting_followup" and state.first is not None:
        # Interim feedback is visible; the rubric/model answer stay hidden
        # because the question is still open.
        public.first_answer = state.first.answer
        public.first_evaluation = state.first.evaluation
        public.followup_question = state.first.evaluation.followup_question
    if state.status in ("completed", "skipped"):
        public.model_answer = q.model_answer
        public.rubric_key_points = q.rubric_key_points
        public.final_score = state.final_score
        if state.first is not None:
            public.first_answer = state.first.answer
            public.first_evaluation = state.first.evaluation
            public.predicted_score = state.first.predicted_score
        if state.followup is not None:
            public.followup_question = state.first.evaluation.followup_question
            public.followup_answer = state.followup.answer
        public.final_evaluation = _final_evaluation(state)
    return public


def _public_session(session: InterviewSession) -> InterviewSessionPublic:
    complete = all(
        q.status in ("completed", "skipped") for q in session.questions
    )
    return InterviewSessionPublic(
        session_id=session.session_id,
        article_id=session.article_id,
        topic=session.topic,
        level=session.level,
        mode=session.mode,
        job_profile_id=session.job_profile_id,
        duration_minutes=session.duration_minutes,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        complete=complete,
        questions=[
            _public_question(q, session.mode, complete)
            for q in session.questions
        ],
        summary=session.summary,
    )


def _client_for_session(meta_request: dict, topic: str):
    """Article mode reuses the article's original provider pin + preset;
    topic mode gets default auto resolution."""
    try:
        request = ArticleRequest.model_validate(meta_request or {"topic": topic})
    except Exception:
        request = ArticleRequest(topic=topic)
    return _anthropic_client(request), request.model_preset


class InterviewCreateRequest(BaseModel):
    topic: str | None = None       # topic-only mode
    article_id: str | None = None  # article mode; topic derived from meta.json
    level: str | None = None       # default: article's level, else "intermediate"
    num_questions: int = Field(default=5, ge=3, le=8)
    mode: InterviewMode = "practice"
    # Job mode only:
    job_profile_id: str | None = None
    duration_minutes: int = Field(default=45, ge=15, le=60)


class InterviewAnswerRequest(BaseModel):
    question_id: str
    answer: str = ""
    skip: bool = False
    # Confidence calibration: the candidate's self-predicted score, captured
    # BEFORE feedback is shown (the UI enforces the ordering; the server
    # just stores it alongside the answer).
    predicted_score: int | None = Field(default=None, ge=0, le=10)


class InterviewAnswerResponse(BaseModel):
    question: InterviewQuestionPublic
    evaluation: AnswerEvaluation | None  # None on skip
    followup_question: str | None        # non-null → UI collects one more answer
    session_complete: bool
    summary: InterviewSummary | None


@app.post("/interviews", response_model=InterviewSessionPublic)
async def create_interview(body: InterviewCreateRequest) -> InterviewSessionPublic:
    if body.mode == "job":
        if not body.job_profile_id:
            raise HTTPException(
                status_code=422, detail="job_profile_id is required for mode='job'"
            )
        profile, analysis = _load_job_profile(body.job_profile_id)
        topic = profile.role_title + (
            f" @ {profile.company}" if profile.company else ""
        )
        client, preset = _client_for_session({}, topic)
        patterns = await research_job_questions(profile)
        question_set = await generate_job_interview(
            profile=profile,
            analysis=analysis,
            patterns=patterns,
            duration_minutes=body.duration_minutes,
            client=client,
            preset=preset,
        )
        session = InterviewSession(
            session_id=(
                datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
            ),
            topic=topic,
            level="advanced" if "senior" in profile.seniority.lower() else "intermediate",
            mode="job",
            job_profile_id=body.job_profile_id,
            duration_minutes=body.duration_minutes,
            questions=[
                InterviewQuestionState(question=q) for q in question_set.questions
            ],
        )
        _save_session(session)
        return _public_session(session)

    if bool(body.topic) == bool(body.article_id):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of 'topic' or 'article_id'",
        )

    article_markdown: str | None = None
    verified_findings: list[str] = []
    meta_request: dict = {}

    if body.article_id:
        if not _ARTICLE_DIR_PATTERN.match(body.article_id):
            raise HTTPException(status_code=404, detail="Article not found")
        article_dir = OUTPUT_ROOT / body.article_id
        if not article_dir.is_dir():
            raise HTTPException(status_code=404, detail="Article not found")
        meta = _read_article_meta(article_dir)
        if meta is None:
            raise HTTPException(status_code=404, detail="Article metadata missing")
        meta_request = meta.get("request") or {}
        topic = meta_request.get("topic") or body.article_id
        level = body.level or meta_request.get("explanation_level") or "intermediate"
        if level not in _VALID_LEVELS:
            raise HTTPException(status_code=400, detail=f"Invalid level '{level}'")
        level_file = article_dir / f"{level}.md"
        if not level_file.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"Level '{level}' not generated for this article",
            )
        article_markdown = level_file.read_text(encoding="utf-8")
        # Older articles may predate verification_reports — degrade to [].
        verified_findings = [
            r["verifier_note"]
            for r in meta.get("verification_reports") or []
            if r.get("support_status") == "supported" and r.get("verifier_note")
        ]
    else:
        topic = body.topic.strip()
        if not topic:
            raise HTTPException(status_code=422, detail="Topic is empty")
        level = body.level or "intermediate"
        if level not in _VALID_LEVELS:
            raise HTTPException(status_code=400, detail=f"Invalid level '{level}'")

    client, preset = _client_for_session(meta_request, topic)

    # Real-interview grounding: what interviewers actually ask about this
    # topic right now. Degrades to [] with no search keys configured.
    patterns = await find_real_question_patterns(topic, level)

    question_set = await generate_interview_questions(
        topic=topic,
        level=level,
        client=client,
        preset=preset,
        num_questions=body.num_questions,
        question_patterns=patterns,
        article_markdown=article_markdown,
        verified_findings=verified_findings or None,
        mode=body.mode,
    )

    session = InterviewSession(
        session_id=(
            datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        ),
        article_id=body.article_id,
        topic=topic,
        level=level,
        mode=body.mode,
        questions=[
            InterviewQuestionState(question=q) for q in question_set.questions
        ],
    )
    _save_session(session)
    return _public_session(session)


def _iter_sessions() -> list[tuple[InterviewSession, float]]:
    """All readable sessions as (session, file mtime) pairs. Corrupt files
    are skipped silently, same as article history."""
    root = _sessions_root()
    if not root.is_dir():
        return []
    pairs: list[tuple[InterviewSession, float]] = []
    for path in root.glob("*.json"):
        try:
            session = InterviewSession.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception:
            continue
        pairs.append((session, path.stat().st_mtime))
    return pairs


@app.get("/interviews", response_model=list[InterviewSessionSummaryItem])
async def list_interviews(
    article_id: str | None = None, limit: int = 50
) -> list[InterviewSessionSummaryItem]:
    items: list[tuple[InterviewSessionSummaryItem, float]] = []
    for session, mtime in _iter_sessions():
        if article_id is not None and session.article_id != article_id:
            continue
        answered = [q for q in session.questions if q.status == "completed"]
        scores = [q.final_score for q in answered if q.final_score is not None]
        items.append((
            InterviewSessionSummaryItem(
                session_id=session.session_id,
                article_id=session.article_id,
                topic=session.topic,
                level=session.level,
                mode=session.mode,
                job_profile_id=session.job_profile_id,
                created_at=session.created_at.isoformat(),
                complete=all(
                    q.status in ("completed", "skipped")
                    for q in session.questions
                ),
                answered=len(answered),
                total=len(session.questions),
                average_score=(
                    round(sum(scores) / len(scores), 1) if scores else None
                ),
            ),
            mtime,
        ))
    items.sort(key=lambda pair: pair[1], reverse=True)
    return [item for item, _ in items[:limit]]


class TopicStats(BaseModel):
    topic: str
    sessions: int
    average_score: float | None
    # 0-100, recent-weighted (last 3 sessions of the topic count double) —
    # the dashboard mastery bar.
    mastery: int = 0


class WeakSectionStat(BaseModel):
    section: str
    count: int


class BadgeStat(BaseModel):
    id: str
    label: str
    earned: bool


class InterviewStats(BaseModel):
    total_sessions: int
    completed_sessions: int
    total_answered: int
    average_score: float | None
    per_topic: list[TopicStats]
    weak_sections: list[WeakSectionStat]
    # Per-session average of the last 10 completed sessions, oldest → newest,
    # for the dashboard trend sparkline.
    recent_scores: list[float]
    # Consecutive calendar days (ending today or yesterday) with at least
    # one completed session.
    streak_days: int = 0
    badges: list[BadgeStat] = []
    # Mean |predicted - actual| across ALL answered questions that carried
    # a prediction, any session. None until predictions exist.
    calibration_gap: float | None = None


def _compute_streak(dates: set) -> int:
    """Consecutive-day streak ending today (or yesterday, so an evening
    practice yesterday still shows a live streak this morning).

    Anchored on the UTC date because session.created_at is stored in UTC —
    mixing local today with UTC session dates made evening sessions look
    like they happened 'tomorrow' and zeroed the streak."""
    from datetime import timedelta as _td
    today = datetime.utcnow().date()
    anchor = today if today in dates else today - _td(days=1)
    if anchor not in dates:
        return 0
    streak = 0
    day = anchor
    while day in dates:
        streak += 1
        day -= _td(days=1)
    return streak


# NOTE: registered before GET /interviews/{session_id} — FastAPI matches
# routes in registration order, so "/interviews/stats" must come first or
# it would be captured as session_id="stats".
@app.get("/interviews/stats", response_model=InterviewStats)
async def interview_stats() -> InterviewStats:
    """Deterministic aggregates over every stored session — the data behind
    the practice dashboard (tiles, sparkline, mastery bars, streak, badges)."""
    pairs = _iter_sessions()

    all_scores: list[int] = []
    total_answered = 0
    completed_sessions: list[tuple[float, float]] = []  # (mtime, session avg)
    completed_drills = 0
    # topic → list of (mtime, session average) for recent-weighted mastery
    topic_session_avgs: dict[str, list[tuple[float, float]]] = {}
    topic_counts: dict[str, int] = {}
    weak_counts: dict[str, int] = {}
    practice_dates: set = set()
    calibration_deltas: list[int] = []
    any_strong = False

    for session, mtime in pairs:
        answered = [q for q in session.questions if q.status == "completed"]
        total_answered += len(answered)
        scores = [q.final_score for q in answered if q.final_score is not None]
        all_scores.extend(scores)
        topic_counts[session.topic] = topic_counts.get(session.topic, 0) + 1
        any_strong = any_strong or any(s >= 8 for s in scores)

        for state in answered:
            if (
                state.first is not None
                and state.first.predicted_score is not None
                and state.final_score is not None
            ):
                calibration_deltas.append(
                    abs(state.first.predicted_score - state.final_score)
                )

        is_complete = session.questions and all(
            q.status in ("completed", "skipped") for q in session.questions
        )
        if is_complete and scores:
            session_avg = sum(scores) / len(scores)
            completed_sessions.append((mtime, session_avg))
            topic_session_avgs.setdefault(session.topic, []).append(
                (mtime, session_avg)
            )
            practice_dates.add(session.created_at.date())
            if session.mode == "drill":
                completed_drills += 1

        for state in answered:
            if state.final_score is None or state.final_score > 6:
                continue
            evaluation = _final_evaluation(state)
            if evaluation is None:
                continue
            for pointer in evaluation.section_pointers:
                weak_counts[pointer] = weak_counts.get(pointer, 0) + 1

    def _mastery(session_avgs: list[tuple[float, float]]) -> int:
        """Recent-weighted 0-100: the last 3 sessions count double, so
        improvement shows quickly and one old bad run doesn't haunt."""
        if not session_avgs:
            return 0
        ordered = sorted(session_avgs, key=lambda p: p[0])  # oldest first
        weighted, weights = 0.0, 0.0
        recent_cutoff = max(0, len(ordered) - 3)
        for i, (_, avg) in enumerate(ordered):
            w = 2.0 if i >= recent_cutoff else 1.0
            weighted += avg * w
            weights += w
        return min(100, round((weighted / weights) * 10))

    per_topic = []
    for topic, count in topic_counts.items():
        avgs = topic_session_avgs.get(topic, [])
        flat = [a for _, a in avgs]
        per_topic.append(TopicStats(
            topic=topic,
            sessions=count,
            average_score=(round(sum(flat) / len(flat), 1) if flat else None),
            mastery=_mastery(avgs),
        ))

    streak = _compute_streak(practice_dates)
    calibration = (
        round(sum(calibration_deltas) / len(calibration_deltas), 1)
        if calibration_deltas else None
    )
    topic_mastered = any(
        t.sessions >= 3 and t.mastery >= 80 for t in per_topic
    )
    badges = [
        BadgeStat(id="first-session", label="First interview",
                  earned=len(completed_sessions) >= 1),
        BadgeStat(id="first-strong", label="First strong answer (8+)",
                  earned=any_strong),
        BadgeStat(id="five-sessions", label="5 sessions done",
                  earned=len(completed_sessions) >= 5),
        BadgeStat(id="streak-3", label="3-day streak", earned=streak >= 3),
        BadgeStat(id="streak-7", label="7-day streak", earned=streak >= 7),
        BadgeStat(id="topic-mastered", label="Topic mastered",
                  earned=topic_mastered),
        BadgeStat(id="well-calibrated", label="Well calibrated",
                  earned=(calibration is not None
                          and len(calibration_deltas) >= 5
                          and calibration <= 1.5)),
        BadgeStat(id="drill-sergeant", label="3 drills done",
                  earned=completed_drills >= 3),
    ]

    completed_sessions.sort(key=lambda p: p[0])  # oldest first
    return InterviewStats(
        total_sessions=len(pairs),
        completed_sessions=len(completed_sessions),
        total_answered=total_answered,
        average_score=(
            round(sum(all_scores) / len(all_scores), 1) if all_scores else None
        ),
        per_topic=per_topic,
        weak_sections=[
            WeakSectionStat(section=s, count=c)
            for s, c in sorted(
                weak_counts.items(), key=lambda kv: kv[1], reverse=True
            )[:8]
        ],
        recent_scores=[round(avg, 1) for _, avg in completed_sessions[-10:]],
        streak_days=streak,
        badges=badges,
        calibration_gap=calibration,
    )


@app.get("/interviews/{session_id}", response_model=InterviewSessionPublic)
async def get_interview(session_id: str) -> InterviewSessionPublic:
    return _public_session(_load_session(session_id))


def _article_markdown_for(session: InterviewSession) -> str | None:
    """Re-read the article text for grading grounding. A deleted article
    degrades to None (topic-mode grading) rather than failing the answer."""
    if not session.article_id:
        return None
    path = OUTPUT_ROOT / session.article_id / f"{session.level}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


@app.post(
    "/interviews/{session_id}/answers",
    response_model=InterviewAnswerResponse,
)
async def submit_interview_answer(
    session_id: str, body: InterviewAnswerRequest
) -> InterviewAnswerResponse:
    async with _session_lock(session_id):
        session = _load_session(session_id)
        state = next(
            (q for q in session.questions if q.question.id == body.question_id),
            None,
        )
        if state is None:
            raise HTTPException(status_code=404, detail="Question not found")
        if state.status in ("completed", "skipped"):
            raise HTTPException(
                status_code=409, detail="Question already answered"
            )

        evaluation: AnswerEvaluation | None = None

        if body.skip:
            if state.status == "awaiting_followup":
                # Skipping mid-follow-up finalizes with the first evaluation
                # rather than discarding the answer the user already gave.
                state.status = "completed"
                state.final_score = state.first.evaluation.score
            else:
                state.status = "skipped"
        else:
            answer = body.answer.strip()
            if not answer:
                raise HTTPException(
                    status_code=422,
                    detail="Answer is empty (use skip=true to skip)",
                )
            meta_request: dict = {}
            if session.article_id:
                meta = _read_article_meta(OUTPUT_ROOT / session.article_id)
                meta_request = (meta or {}).get("request") or {}
            client, preset = _client_for_session(meta_request, session.topic)
            article_markdown = _article_markdown_for(session)
            # Job mode grades against the JD/resume instead of an article.
            job_context = None
            if session.mode == "job" and session.job_profile_id:
                try:
                    job_profile, _ = _load_job_profile(session.job_profile_id)
                    job_context = _job_context_for(job_profile, state.question)
                except HTTPException:
                    job_context = None  # profile deleted: grade ungrounded

            # Interviewer memory: a digest of prior answers so grading has
            # session continuity (consistency checks, "as you said earlier"
            # follow-ups). Drills stay memoryless — rapid fire is meant to
            # test each item cold, and the digest would just burn tokens.
            session_memory = None
            if session.mode != "drill":
                session_memory = (
                    interview_memory_digest(session, state.question.id) or None
                )

            if state.status == "pending":
                evaluation = await evaluate_answer(
                    question=state.question,
                    user_answer=answer,
                    level=session.level,
                    client=client,
                    preset=preset,
                    article_markdown=article_markdown,
                    job_context=job_context,
                    session_memory=session_memory,
                )
                # Follow-ups exist in practice AND job modes (real
                # interviewers probe); simulations never break their flow
                # for coaching, and drills stay rapid-fire.
                if session.mode in ("simulation", "drill") and evaluation.needs_followup:
                    evaluation = evaluation.model_copy(
                        update={"needs_followup": False, "followup_question": ""}
                    )
                state.first = InterviewAnswerRecord(
                    answer=answer,
                    evaluation=evaluation,
                    predicted_score=body.predicted_score,
                )
                if evaluation.needs_followup and evaluation.followup_question:
                    state.status = "awaiting_followup"
                else:
                    state.status = "completed"
                    state.final_score = evaluation.score
            else:  # awaiting_followup — combined final evaluation
                evaluation = await evaluate_answer(
                    question=state.question,
                    user_answer=state.first.answer,
                    level=session.level,
                    client=client,
                    preset=preset,
                    article_markdown=article_markdown,
                    job_context=job_context,
                    session_memory=session_memory,
                    followup_question=state.first.evaluation.followup_question,
                    followup_answer=answer,
                )
                state.followup = InterviewAnswerRecord(
                    answer=answer, evaluation=evaluation
                )
                state.status = "completed"
                state.final_score = evaluation.score

        session.updated_at = datetime.utcnow()
        session_complete = all(
            q.status in ("completed", "skipped") for q in session.questions
        )
        if session_complete and session.summary is None:
            session.summary = _compute_summary(session)
            if session.mode == "simulation":
                # Interviewer's narrative verdict — one small LLM call.
                # Failure-tolerant garnish: never block session completion.
                try:
                    meta_request = {}
                    if session.article_id:
                        meta = _read_article_meta(OUTPUT_ROOT / session.article_id)
                        meta_request = (meta or {}).get("request") or {}
                    client, preset = _client_for_session(meta_request, session.topic)
                    results = []
                    for q in session.questions:
                        ev = _final_evaluation(q)
                        results.append({
                            "question": q.question.question,
                            "score": q.final_score,
                            "verdict": ev.verdict if ev else "skipped",
                            "gaps": ev.gaps if ev else [],
                            "strength": (ev.strengths[0] if ev and ev.strengths else ""),
                        })
                    session.summary.debrief = await generate_debrief(
                        topic=session.topic, level=session.level,
                        results=results, client=client, preset=preset,
                    )
                except Exception:
                    logging.exception("Debrief generation failed; continuing without it")
            elif session.mode == "job" and session.job_profile_id:
                # Recruiter-grade scorecard: competency rollup + panel
                # debrief + cited study plan. Failure-tolerant garnish.
                try:
                    profile, analysis = _load_job_profile(session.job_profile_id)
                    client, preset = _client_for_session({}, session.topic)
                    scorecard = await generate_job_scorecard(
                        session=session, profile=profile, analysis=analysis,
                        client=client, preset=preset,
                    )
                    session.summary.scorecard = scorecard
                    session.summary.debrief = scorecard.debrief
                except Exception:
                    logging.exception("Scorecard generation failed; continuing without it")
        _save_session(session)

        followup = (
            evaluation.followup_question
            if evaluation is not None
            and state.status == "awaiting_followup"
            else None
        )
        # Simulation/job redaction: mid-screen answers return no evaluation —
        # the reveal happens all at once on the debrief/scorecard screen.
        visible_evaluation = (
            None
            if session.mode in ("simulation", "job") and not session_complete
            else evaluation
        )
        return InterviewAnswerResponse(
            question=_public_question(state, session.mode, session_complete),
            evaluation=visible_evaluation,
            followup_question=followup,
            session_complete=session_complete,
            summary=session.summary if session_complete else None,
        )


# ── Job-targeted interviews (resume + JD) ────────────────────────────
# A JobProfile captures one target job (role, company, JD, resume). The
# analysis derives the 5-8 competencies that become the interview's rubric
# AND the scorecard's rows. Sessions with mode="job" run simulation-style
# (no feedback until the end) but keep follow-ups, and finish with a
# recruiter-grade scorecard including a cited study plan.

_JOB_PROFILES_DIR = "job_profiles"


def _job_profiles_root() -> Path:
    return OUTPUT_ROOT / _JOB_PROFILES_DIR


def _job_profile_path(profile_id: str) -> Path:
    return _job_profiles_root() / f"{profile_id}.json"


def _load_job_profile(profile_id: str) -> tuple[JobProfile, JobAnalysis]:
    if not _SESSION_ID_PATTERN.match(profile_id):
        raise HTTPException(status_code=404, detail="Job profile not found")
    path = _job_profile_path(profile_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Job profile not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            JobProfile.model_validate(data["profile"]),
            JobAnalysis.model_validate(data["analysis"]),
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=410, detail="Job profile unreadable")


def _save_job_profile(profile: JobProfile, analysis: JobAnalysis) -> None:
    root = _job_profiles_root()
    root.mkdir(parents=True, exist_ok=True)
    path = _job_profile_path(profile.profile_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "profile": profile.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"),
    }, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class JobProfileCreateRequest(BaseModel):
    role_title: str
    company: str = ""
    location: str = ""
    seniority: str = ""
    extra_notes: str = ""
    # JD: pasted text OR a URL to fetch.
    job_description: str = ""
    jd_url: str = ""
    # Resume: pasted text OR an uploaded file (base64, like /transcribe).
    resume_text: str = ""
    resume_file_b64: str = ""
    resume_filename: str = ""


class JobProfileResponse(BaseModel):
    profile: JobProfile
    analysis: JobAnalysis


class JobProfileSummary(BaseModel):
    profile_id: str
    role_title: str
    company: str
    location: str
    seniority: str
    created_at: str


async def _resolve_jd_text(body: JobProfileCreateRequest) -> str:
    if body.job_description.strip():
        return body.job_description.strip()[:30_000]
    if body.jd_url.strip():
        from pipeline.workers.extraction_worker import (
            fetch_with_retry, injection_filter, remove_boilerplate,
        )
        try:
            raw, _strategy = await fetch_with_retry(body.jd_url.strip())
            text = injection_filter(remove_boilerplate(raw)).strip()
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Could not fetch the job posting URL — paste the JD text instead.",
            )
        if len(text) < 200:
            raise HTTPException(
                status_code=422,
                detail="The job posting page yielded almost no text (likely "
                       "behind a login or rendered by JavaScript) — paste the JD text instead.",
            )
        return text[:30_000]
    raise HTTPException(status_code=422, detail="Provide the job description (text or URL)")


def _resolve_resume_text(body: JobProfileCreateRequest) -> str:
    if body.resume_text.strip():
        return body.resume_text.strip()[:30_000]
    if body.resume_file_b64:
        try:
            data = base64.b64decode(body.resume_file_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="resume_file_b64 is not valid base64")
        try:
            return parse_resume(data, body.resume_filename)
        except ResumeParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    raise HTTPException(status_code=422, detail="Provide a resume (file upload or pasted text)")


@app.post("/job-profiles", response_model=JobProfileResponse)
async def create_job_profile(body: JobProfileCreateRequest) -> JobProfileResponse:
    if not body.role_title.strip():
        raise HTTPException(status_code=422, detail="role_title is required")
    jd_text = await _resolve_jd_text(body)
    resume_text = _resolve_resume_text(body)

    profile = JobProfile(
        profile_id=(
            datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        ),
        role_title=body.role_title.strip(),
        company=body.company.strip(),
        location=body.location.strip(),
        seniority=body.seniority.strip(),
        job_description=jd_text,
        resume_text=resume_text,
        extra_notes=body.extra_notes.strip(),
    )
    client, preset = _client_for_session({}, profile.role_title)
    analysis = await analyze_job_fit(profile, client, preset)
    _save_job_profile(profile, analysis)
    return JobProfileResponse(profile=profile, analysis=analysis)


@app.get("/job-profiles", response_model=list[JobProfileSummary])
async def list_job_profiles() -> list[JobProfileSummary]:
    root = _job_profiles_root()
    if not root.is_dir():
        return []
    items: list[tuple[JobProfileSummary, float]] = []
    for path in root.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            p = JobProfile.model_validate(data["profile"])
        except Exception:
            continue
        items.append((JobProfileSummary(
            profile_id=p.profile_id, role_title=p.role_title,
            company=p.company, location=p.location, seniority=p.seniority,
            created_at=p.created_at.isoformat(),
        ), path.stat().st_mtime))
    items.sort(key=lambda pair: pair[1], reverse=True)
    return [item for item, _ in items]


@app.get("/job-profiles/{profile_id}", response_model=JobProfileResponse)
async def get_job_profile(profile_id: str) -> JobProfileResponse:
    profile, analysis = _load_job_profile(profile_id)
    return JobProfileResponse(profile=profile, analysis=analysis)


@app.delete("/job-profiles/{profile_id}")
async def delete_job_profile(profile_id: str) -> dict:
    if not _SESSION_ID_PATTERN.match(profile_id):
        raise HTTPException(status_code=404, detail="Job profile not found")
    path = _job_profile_path(profile_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Job profile not found")
    path.unlink()
    return {"deleted": profile_id}


def _job_context_for(profile: JobProfile, question) -> str:
    """Grading context for job-mode answers: the JD's bar + the resume the
    candidate must stay consistent with."""
    competency = competency_for_question(question) or "(untagged)"
    return (
        f"role: {profile.role_title} ({profile.seniority or 'unspecified seniority'})"
        f"{' at ' + profile.company if profile.company else ''}\n"
        f"competency under test: {competency}\n\n"
        f"job_description (excerpt):\n{profile.job_description[:4000]}\n\n"
        f"candidate_resume (excerpt):\n{profile.resume_text[:4000]}"
    )


# ── Resume studio ────────────────────────────────────────────────────
# Upload/paste a resume → JSON Resume structure (extracted once, the
# substrate for everything) → transparent ATS checklist + recruiter
# review → optional honest JD tailoring with an auditable change log →
# Markdown/DOCX/JSON Resume downloads.

_RESUMES_DIR = "resumes"


def _resumes_root() -> Path:
    return OUTPUT_ROOT / _RESUMES_DIR


def _resume_path(resume_id: str) -> Path:
    return _resumes_root() / f"{resume_id}.json"


def _load_resume_doc(resume_id: str) -> ResumeDoc:
    if not _SESSION_ID_PATTERN.match(resume_id):
        raise HTTPException(status_code=404, detail="Resume not found")
    path = _resume_path(resume_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Resume not found")
    try:
        return ResumeDoc.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=410, detail="Resume document unreadable")


def _save_resume_doc(doc: ResumeDoc) -> None:
    root = _resumes_root()
    root.mkdir(parents=True, exist_ok=True)
    doc.updated_at = datetime.utcnow()
    path = _resume_path(doc.resume_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ResumeCreateRequest(BaseModel):
    # Resume: pasted text OR an uploaded file. A .json upload is treated
    # as a JSON Resume document (Reactive Resume-compatible import) and
    # skips LLM extraction entirely.
    resume_text: str = ""
    resume_file_b64: str = ""
    resume_filename: str = ""
    # JD, all optional (the report runs without one): pasted text, a URL
    # to fetch, or a saved job target from the Job prep studio.
    jd_text: str = ""
    jd_url: str = ""
    job_profile_id: str | None = None


class ResumeSummaryItem(BaseModel):
    resume_id: str
    name: str
    jd_label: str
    score: int | None = None
    tailored_score: int | None = None
    created_at: str


async def _resolve_resume_jd(body: ResumeCreateRequest) -> tuple[str, str]:
    """Returns (jd_text, jd_label). Empty text = no JD, which is fine."""
    if body.job_profile_id:
        profile, _analysis = _load_job_profile(body.job_profile_id)
        label = profile.role_title + (f" @ {profile.company}" if profile.company else "")
        return profile.job_description, label
    if body.jd_text.strip():
        return body.jd_text.strip()[:30_000], "pasted JD"
    if body.jd_url.strip():
        from pipeline.workers.extraction_worker import (
            fetch_with_retry, injection_filter, remove_boilerplate,
        )
        try:
            raw, _strategy = await fetch_with_retry(body.jd_url.strip())
            text = injection_filter(remove_boilerplate(raw)).strip()
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Could not fetch the job posting URL — paste the JD text instead.",
            )
        if len(text) < 200:
            raise HTTPException(
                status_code=422,
                detail="The job posting page yielded almost no text (likely behind "
                       "a login or rendered by JavaScript) — paste the JD text instead.",
            )
        return text[:30_000], body.jd_url.strip()
    return "", ""


def _resolve_resume_input(body: ResumeCreateRequest) -> tuple[str, StructuredResume | None]:
    """Returns (resume_text, structure). structure is non-None only for a
    JSON Resume upload, which needs no LLM extraction."""
    if body.resume_text.strip():
        return body.resume_text.strip()[:30_000], None
    if body.resume_file_b64:
        try:
            data = base64.b64decode(body.resume_file_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="resume_file_b64 is not valid base64")
        if (body.resume_filename or "").lower().endswith(".json"):
            try:
                structured = from_jsonresume(json.loads(data.decode("utf-8")))
            except Exception:
                raise HTTPException(
                    status_code=422,
                    detail="Could not read this file as a JSON Resume document "
                           "(jsonresume.org format).",
                )
            return render_markdown(structured), structured
        try:
            return parse_resume(data, body.resume_filename), None
        except ResumeParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    raise HTTPException(status_code=422, detail="Provide a resume (file upload or pasted text)")


async def _finish_resume_analysis(resume_id: str) -> None:
    """Background phase of analysis: the two LLM calls. The deterministic
    report already shipped in the POST response; this fills in structure +
    recruiter review and flips status to ready/error."""
    try:
        doc = _load_resume_doc(resume_id)
    except HTTPException:
        return  # deleted before the task ran
    client, preset = _client_for_session({}, "resume")

    async def _extract() -> StructuredResume | None:
        try:
            return await extract_resume(doc.original_text, client, preset)
        except Exception:
            # A failed extraction degrades to text-only checks — the report
            # still ships, minus the structure-aware rows and tailoring.
            logging.exception("Resume extraction failed; continuing text-only")
            return None

    try:
        # Extraction and review both read only the raw text — run them
        # concurrently (the analyze wait is dominated by these two calls).
        if doc.structured is None:
            doc.structured, doc.review = await asyncio.gather(
                _extract(),
                review_resume(doc.original_text, doc.jd_text, client, preset),
            )
        else:  # JSON Resume import: structure already known
            doc.review = await review_resume(
                doc.original_text, doc.jd_text, client, preset
            )
        # Structure-aware checks join the report now that structure exists.
        doc.report = run_ats_checks(
            doc.original_text, doc.jd_text or None, doc.structured
        )
        doc.status, doc.error = "ready", ""
    except Exception:
        logging.exception("Resume review failed")
        doc.status = "error"
        doc.error = ("The recruiter review failed — check your provider in "
                     "Settings and re-analyze. The checklist above is still valid.")
    if _resume_path(resume_id).is_file():  # deleted mid-analysis → discard
        _save_resume_doc(doc)


@app.post("/resumes", response_model=ResumeDoc)
async def create_resume(
    body: ResumeCreateRequest, background_tasks: BackgroundTasks
) -> ResumeDoc:
    resume_text, structured = _resolve_resume_input(body)
    jd_text, jd_label = await _resolve_resume_jd(body)

    # Everything deterministic ships in THIS response, sub-second: the
    # user sees a real report immediately while the LLM phase runs behind.
    doc = ResumeDoc(
        resume_id=(datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]),
        original_text=resume_text,
        status="analyzing",
        structured=structured,
        jd_text=jd_text,
        jd_label=jd_label,
        report=run_ats_checks(resume_text, jd_text or None, structured),
    )
    _save_resume_doc(doc)
    background_tasks.add_task(_finish_resume_analysis, doc.resume_id)
    return doc


@app.get("/resumes", response_model=list[ResumeSummaryItem])
async def list_resumes() -> list[ResumeSummaryItem]:
    root = _resumes_root()
    if not root.is_dir():
        return []
    items: list[tuple[ResumeSummaryItem, float]] = []
    for path in root.glob("*.json"):
        try:
            doc = ResumeDoc.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append((ResumeSummaryItem(
            resume_id=doc.resume_id,
            name=doc.structured.basics.name if doc.structured else "",
            jd_label=doc.jd_label,
            score=doc.report.score if doc.report else None,
            tailored_score=doc.tailored_report.score if doc.tailored_report else None,
            created_at=doc.created_at.isoformat(),
        ), path.stat().st_mtime))
    items.sort(key=lambda pair: pair[1], reverse=True)
    return [item for item, _ in items]


@app.get("/resumes/{resume_id}", response_model=ResumeDoc)
async def get_resume(resume_id: str) -> ResumeDoc:
    return _load_resume_doc(resume_id)


@app.delete("/resumes/{resume_id}")
async def delete_resume(resume_id: str) -> dict:
    if not _SESSION_ID_PATTERN.match(resume_id):
        raise HTTPException(status_code=404, detail="Resume not found")
    path = _resume_path(resume_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Resume not found")
    path.unlink()
    return {"deleted": resume_id}


async def _finish_resume_tailor(resume_id: str) -> None:
    try:
        doc = _load_resume_doc(resume_id)
    except HTTPException:
        return
    client, preset = _client_for_session({}, "resume")
    try:
        previous = doc.tailored.model_copy(deep=True) if doc.tailored else None
        doc.tailored = await tailor_resume(
            structured=doc.structured,
            jd_text=doc.jd_text,
            review=doc.review,
            report=doc.report,
            client=client,
            preset=preset,
        )
        if previous is not None:
            _push_tailored_history(doc, previous)
        # Before/after on identical footing: re-run the same deterministic
        # checks on the tailored structure's canonical rendering.
        doc.tailored_report = run_ats_checks(
            render_markdown(doc.tailored.resume), doc.jd_text, doc.tailored.resume
        )
        doc.tailor_status, doc.tailor_error = "idle", ""
    except Exception:
        logging.exception("Resume tailoring failed")
        doc.tailor_status = "error"
        doc.tailor_error = ("Tailoring failed — check your provider in Settings "
                            "and try again.")
    if _resume_path(resume_id).is_file():
        _save_resume_doc(doc)


@app.post("/resumes/{resume_id}/tailor", response_model=ResumeDoc)
async def tailor_resume_endpoint(
    resume_id: str, background_tasks: BackgroundTasks
) -> ResumeDoc:
    doc = _load_resume_doc(resume_id)
    if not doc.jd_text:
        raise HTTPException(
            status_code=422,
            detail="This resume has no job description — re-analyze with a JD to tailor.",
        )
    if doc.structured is None:
        raise HTTPException(
            status_code=422,
            detail="No structured resume available (extraction failed) — tailoring "
                   "needs the structure. Try re-uploading.",
        )
    if doc.status == "analyzing":
        raise HTTPException(
            status_code=409, detail="Analysis is still running — tailor once it finishes.",
        )
    if doc.tailor_status == "tailoring":
        raise HTTPException(status_code=409, detail="Tailoring is already running.")
    doc.tailor_status, doc.tailor_error = "tailoring", ""
    _save_resume_doc(doc)
    background_tasks.add_task(_finish_resume_tailor, resume_id)
    return doc


class MetricFillRequest(BaseModel):
    # One entry per [METRIC] occurrence in canonical order; empty strings
    # leave that placeholder for later.
    values: list[str]


@app.post("/resumes/{resume_id}/fill-metrics", response_model=ResumeDoc)
async def fill_resume_metrics(resume_id: str, body: MetricFillRequest) -> ResumeDoc:
    doc = _load_resume_doc(resume_id)
    if doc.tailored is None:
        raise HTTPException(status_code=422, detail="No tailored version to fill yet.")
    if doc.tailor_status == "tailoring":
        raise HTTPException(status_code=409, detail="Tailoring is still running.")
    snapshot = doc.tailored.model_copy(deep=True)
    filled = fill_metric_placeholders(doc.tailored.resume, body.values)
    if filled:
        _push_tailored_history(doc, snapshot)
        # The numbers change quantification/keyword math — keep the
        # before/after comparison honest.
        doc.tailored_report = run_ats_checks(
            render_markdown(doc.tailored.resume), doc.jd_text or None,
            doc.tailored.resume,
        )
        _save_resume_doc(doc)
    return doc


def _push_tailored_history(doc: ResumeDoc, snapshot: TailoredResume) -> None:
    """Every mutation of the tailored version banks the prior state for
    undo. Capped so the doc file cannot grow without bound."""
    doc.tailored_history.append(snapshot)
    del doc.tailored_history[:-10]


def _refresh_tailored_report(doc: ResumeDoc) -> None:
    doc.tailored_report = run_ats_checks(
        render_markdown(doc.tailored.resume), doc.jd_text or None,
        doc.tailored.resume,
    )


class TailoredEditItem(BaseModel):
    path: str
    value: str


class TailoredEditRequest(BaseModel):
    edits: list[TailoredEditItem]


@app.post("/resumes/{resume_id}/edit-tailored", response_model=ResumeDoc)
async def edit_tailored_endpoint(
    resume_id: str, body: TailoredEditRequest
) -> ResumeDoc:
    """The user's own text edits to the tailored resume, by where-path."""
    doc = _load_resume_doc(resume_id)
    if doc.tailored is None:
        raise HTTPException(status_code=422, detail="No tailored version to edit yet.")
    if doc.tailor_status == "tailoring":
        raise HTTPException(status_code=409, detail="Tailoring is still running.")
    if not body.edits:
        raise HTTPException(status_code=422, detail="No edits given.")
    snapshot = doc.tailored.model_copy(deep=True)
    try:
        applied = apply_tailored_edits(
            doc.tailored.resume, [(e.path, e.value) for e in body.edits]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if applied:
        _push_tailored_history(doc, snapshot)
        doc.tailored.changes.extend(
            ResumeChange(
                kind="rephrased", where=e.path,
                what=("Edited by you, directly on the paper."
                      if e.value.strip() else "Removed by you."),
            )
            for e in body.edits
        )
        _refresh_tailored_report(doc)
        _save_resume_doc(doc)
    return doc


class ResumeAdviceRequest(BaseModel):
    question: str
    # Prior turns of this chat, [{role: user|assistant, content: str}];
    # the client keeps the transcript, the server stays stateless.
    history: list[dict] = []


@app.post("/resumes/{resume_id}/advise")
async def advise_resume_endpoint(
    resume_id: str, body: ResumeAdviceRequest
) -> dict:
    """Read-only coaching chat: metrics, phrasing, what recruiters read.
    Never mutates the resume — edits go through /request-edit."""
    doc = _load_resume_doc(resume_id)
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Ask a question first.")
    client, preset = _client_for_session({}, "resume")
    try:
        answer = await advise_resume(
            question=question, history=body.history[-8:],
            context=build_resume_advice_context(doc),
            client=client, preset=preset,
        )
    except Exception:
        logging.exception("Resume advice failed")
        raise HTTPException(
            status_code=502,
            detail="The coach is unavailable — check your provider in Settings.",
        )
    return {"answer": answer}


class ResumeInstructionRequest(BaseModel):
    instruction: str = ""
    # Coach chat turns; with an empty instruction the edit applies what
    # the coach recommended in this conversation.
    history: list[dict] = []


@app.post("/resumes/{resume_id}/request-edit", response_model=ResumeDoc)
async def request_tailored_edit(
    resume_id: str, body: ResumeInstructionRequest
) -> ResumeDoc:
    """Apply one natural-language instruction to the tailored resume via
    the LLM, behind the same honesty guard as tailoring. The prior
    version lands on the undo stack."""
    doc = _load_resume_doc(resume_id)
    if doc.tailored is None or doc.structured is None:
        raise HTTPException(status_code=422, detail="No tailored version to edit yet.")
    if doc.tailor_status == "tailoring":
        raise HTTPException(status_code=409, detail="Tailoring is still running.")
    instruction = body.instruction.strip()
    history = [
        m for m in body.history[-8:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not instruction and not history:
        raise HTTPException(status_code=422, detail="Describe the edit you want.")
    if not instruction:
        instruction = (
            "The coaching conversation above recommends specific bullet "
            "rewrites. Apply exactly those: rewrite each bullet the coach "
            "recommended changing, in the recommended form. Where the "
            "rewritten bullet needs a number only the candidate knows, write "
            "[METRIC] in its place and add a warning naming exactly what to "
            "look up and where (e.g. before/after deploy minutes from "
            "pipeline history). Do not touch any field the coach did not "
            "name."
        )
    client, preset = _client_for_session({}, "resume")
    snapshot = doc.tailored.model_copy(deep=True)
    try:
        edited = await edit_resume_by_instruction(
            original=doc.structured, tailored=doc.tailored,
            jd_text=doc.jd_text, instruction=instruction,
            client=client, preset=preset, conversation=history,
        )
    except Exception:
        logging.exception("Instructed resume edit failed")
        raise HTTPException(
            status_code=502,
            detail="The edit did not go through — check your provider in Settings "
                   "and try again.",
        )
    # Belts behind the model: invented numbers revert, and the change log
    # is reconciled against the real diff so the UI never under-reports.
    user_text = body.instruction + " " + " ".join(
        m.get("content", "") for m in history
    )
    edited = guard_edited_numbers_and_log(snapshot, edited, user_text)
    # Belt for the prompt's warnings/note separation: a status message
    # that slipped into warnings is not a durable honesty note — move it.
    status_like = [
        w for w in edited.warnings
        if w.strip().upper().startswith("NO CHANGES") or "this pass" in w.lower()
    ]
    if status_like:
        edited.warnings = [w for w in edited.warnings if w not in status_like]
        if not edited.note:
            edited.note = " ".join(status_like)
    # The change log is the document's full history: a pass appends its
    # entries, it never replaces what earlier passes recorded.
    edited.changes = snapshot.changes + edited.changes
    _push_tailored_history(doc, snapshot)
    doc.tailored = edited
    _refresh_tailored_report(doc)
    _save_resume_doc(doc)
    return doc


@app.post("/resumes/{resume_id}/undo-tailored", response_model=ResumeDoc)
async def undo_tailored_endpoint(resume_id: str) -> ResumeDoc:
    """Step the tailored resume back to the version before the last
    mutation (metric fill, manual edit, instructed edit, or re-tailor)."""
    doc = _load_resume_doc(resume_id)
    if doc.tailor_status == "tailoring":
        raise HTTPException(status_code=409, detail="Tailoring is still running.")
    if not doc.tailored_history:
        raise HTTPException(status_code=422, detail="Nothing to undo.")
    doc.tailored = doc.tailored_history.pop()
    _refresh_tailored_report(doc)
    _save_resume_doc(doc)
    return doc


_RESUME_DOWNLOADS = {
    "pdf": ("application/pdf", "pdf"),
    "md": ("text/markdown", "md"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    "json": ("application/json", "json"),
}


@app.get("/resumes/{resume_id}/download")
async def download_resume(
    resume_id: str, fmt: str = "md", version: str = "original"
) -> Response:
    if fmt not in _RESUME_DOWNLOADS:
        raise HTTPException(status_code=422, detail="fmt must be pdf, docx, md, or json")
    if version not in ("original", "tailored"):
        raise HTTPException(status_code=422, detail="version must be original or tailored")
    doc = _load_resume_doc(resume_id)
    if version == "tailored":
        if doc.tailored is None:
            raise HTTPException(status_code=404, detail="No tailored version yet")
        structured = doc.tailored.resume
    else:
        if doc.structured is None:
            raise HTTPException(status_code=422, detail="No structured resume available")
        structured = doc.structured
    media_type, ext = _RESUME_DOWNLOADS[fmt]
    if fmt == "pdf":
        payload: bytes = render_pdf(structured)
    elif fmt == "md":
        payload = render_markdown(structured).encode("utf-8")
    elif fmt == "docx":
        payload = render_docx(structured)
    else:
        payload = json.dumps(to_jsonresume(structured), indent=2).encode("utf-8")
    filename = f"resume-{version}-{resume_id}.{ext}"
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Voice transcription ──────────────────────────────────────────────
# Fallback speech-to-text for browsers whose built-in SpeechRecognition is
# missing or unreliable (Safari), and for the opt-in high-accuracy mode.
# Audio arrives as base64 JSON (no python-multipart dependency needed) and
# is transcribed with OpenAI's audio API using the user's existing key.

_TRANSCRIBE_MAX_BYTES = 10 * 1024 * 1024  # decoded audio cap

# mime → filename hint for the OpenAI SDK (it sniffs the format from the
# name). Chrome records audio/webm (opus), Safari records audio/mp4 (AAC).
_AUDIO_FILENAMES = {
    "audio/webm": "audio.webm",
    "audio/mp4": "audio.mp4",
    "audio/mpeg": "audio.mp3",
    "audio/ogg": "audio.ogg",
    "audio/wav": "audio.wav",
}


class TranscribeRequest(BaseModel):
    audio_b64: str
    mime_type: str = "audio/webm"


class TranscribeResponse(BaseModel):
    text: str


def _openai_audio_client():
    """Factory for both transcription (/transcribe) and speech (/speak) so
    tests can monkeypatch server._openai_audio_client (same seam pattern as
    server._anthropic_client). Returns None when no key is configured — the
    endpoints map that to 503."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    import openai
    return openai.AsyncOpenAI()


async def _openai_transcribe(client, audio: bytes, mime_type: str) -> str:
    base_mime = mime_type.split(";")[0].strip().lower()  # drop ";codecs=opus"
    filename = _AUDIO_FILENAMES.get(base_mime, "audio.webm")
    result = await client.audio.transcriptions.create(
        # Read at call time so env changes take effect without a reload.
        model=os.environ.get("TRANSCRIBE_MODEL", "whisper-1"),
        file=(filename, audio),
    )
    return (result.text or "").strip()


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(body: TranscribeRequest) -> TranscribeResponse:
    try:
        audio = base64.b64decode(body.audio_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="audio_b64 is not valid base64")
    if not audio:
        raise HTTPException(status_code=422, detail="Audio is empty")
    if len(audio) > _TRANSCRIBE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Audio exceeds the 10 MB limit — record a shorter answer",
        )
    client = _openai_audio_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "High-accuracy transcription requires OPENAI_API_KEY on the "
                "server. Use browser dictation or type your answer."
            ),
        )
    try:
        text = await _openai_transcribe(client, audio, body.mime_type)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}")
    return TranscribeResponse(text=text)


# ── Interviewer voice (natural TTS) ──────────────────────────────────
# Browser SpeechSynthesis sounds robotic; OpenAI's TTS voices are close to
# human. The client tries POST /speak first and falls back to the browser
# voice when this returns 503 (no key) or errors.

_SPEAK_MAX_CHARS = 2000


class SpeakRequest(BaseModel):
    text: str


@app.post("/speak")
async def speak(body: SpeakRequest) -> Response:
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Text is empty")
    if len(text) > _SPEAK_MAX_CHARS:
        raise HTTPException(
            status_code=413, detail=f"Text exceeds {_SPEAK_MAX_CHARS} characters"
        )
    client = _openai_audio_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="Natural voice requires OPENAI_API_KEY; falling back to browser voice.",
        )
    try:
        result = await client.audio.speech.create(
            model=os.environ.get("TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.environ.get("TTS_VOICE", "nova"),
            input=text,
        )
        data = getattr(result, "content", None)
        if not isinstance(data, (bytes, bytearray)):
            data = result.read()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Speech synthesis failed: {exc}")
    return Response(content=bytes(data), media_type="audio/mpeg")


# ── Static UI ────────────────────────────────────────────────────────
# HTML must never be cached: hashed JS/CSS assets change name per build,
# but a cached index.html keeps pointing at the OLD hashes and users see
# a stale app after every deploy (bitten twice in verification).
@app.middleware("http")
async def _no_html_cache(request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# The React Studio (web/dist, built with `npm run build` in web/) is
# the app. It owns the root; /studio and /desk stay as aliases because
# they shipped first and live in bookmarks. The classic single-file UI
# is parked at /classic for one transition release, then deleted.
_DESK_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"
_UI_DIR = Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.exists():
    # Static mounts do not redirect the bare path to the slash form,
    # so /classic without the trailing slash needs an explicit hop.
    @app.get("/classic", include_in_schema=False)
    async def _classic_redirect() -> RedirectResponse:
        return RedirectResponse("/classic/")

    app.mount("/classic", StaticFiles(directory=str(_UI_DIR), html=True), name="classic")
if _DESK_DIR.exists():
    app.mount("/studio", StaticFiles(directory=str(_DESK_DIR), html=True), name="studio")
    app.mount("/desk", StaticFiles(directory=str(_DESK_DIR), html=True), name="desk")
    # Root goes LAST so every API route above wins the match first.
    app.mount("/", StaticFiles(directory=str(_DESK_DIR), html=True), name="app")
elif _UI_DIR.exists():
    # No React build on disk (fresh clone, no npm): classic UI still works.
    app.mount("/", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
