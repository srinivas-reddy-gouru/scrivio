"""Article jobs: their state, and the record of what happened in them.

What this replaces. A job was a queue in one process's memory. Every
stream subscriber drained that one queue, so two browser tabs split the
events between them and each saw half. There was no record of what had
already been sent, so a reconnect could not catch up, and one that
arrived after the end waited for ever on an empty queue. When the
server stopped, the job stopped existing: not failed, just gone.

What a job is now:

  a log     Every event gets a sequence number and is appended to a log,
            in memory and on disk. Subscribers do not consume it. Each
            holds a cursor and reads forward, so any number of them see
            the same complete sequence, and a reconnect resumes from the
            last number it saw.
  a state   running, complete, failed, cancelled, or interrupted. The
            last four are terminal: nothing moves a job out of one.
            A cancel that arrives first stays a cancel even if the
            pipeline finishes a moment later.
  a record  Written to disk as it changes. A job found "running" on disk
            that this process does not hold was cut short by a restart
            and is marked interrupted. It is NOT started again: a job is
            a run of paid model calls, and spending someone's money is
            not a recovery step. The interface offers the retry.

Scope. This is right for one process on one machine. Several worker
processes would each believe the others' running jobs were interrupted.
Hosting needs a shared store and real workers, which this is not.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from pipeline.schemas.models import ProgressEvent, PublishedArticle

RUNNING, COMPLETE, FAILED, CANCELLED, INTERRUPTED = (
    "running", "complete", "failed", "cancelled", "interrupted")
TERMINAL = frozenset({COMPLETE, FAILED, CANCELLED, INTERRUPTED})

MAX_EVENTS = 5_000              # per job, on disk and in total
MAX_EVENTS_IN_MEMORY = 500      # per job; older ones are read back from disk
KEEP_FINISHED_IN_MEMORY = 20    # jobs
KEEP_ON_DISK = 200              # job records
HEARTBEAT_SECONDS = 15.0

INTERRUPTED_MESSAGE = (
    "This run was cut short when the server stopped. Nothing was saved from "
    "it and nothing is still running. Start it again when you are ready."
)

_root = lambda: Path("./output") / "_jobs"          # noqa: E731


def configure(root) -> None:
    """`root` is a callable, so the directory is looked up when it is
    needed and follows wherever the output directory is pointed."""
    global _root
    _root = root


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise


class Heartbeat:
    """Nothing happened. Sent so a quiet stream is distinguishable from a
    dead one, and so a vanished client is noticed."""


class Gap:
    """Events between `after` and `resumes_at` are no longer held."""

    def __init__(self, after: int, resumes_at: int) -> None:
        self.after, self.resumes_at = after, resumes_at


class Job:
    def __init__(self, job_id: str, *, request_key: str = "", topic: str = "") -> None:
        self.job_id = job_id
        self.request_key = request_key
        self.topic = topic
        self.state = RUNNING
        self.result: dict[str, PublishedArticle] | None = None
        self.error: str | None = None
        self.task: asyncio.Task | None = None
        self.created_at = datetime.now(timezone.utc)
        self.finished_at: float | None = None
        self.last_seq = 0
        self.last_event_at: float | None = time.monotonic()
        self._events: list[tuple[int, ProgressEvent]] = []
        self._changed = asyncio.Condition()
        self._on_disk = False          # loaded from a record, not running here

    # ── compatibility with what the rest of the code reads ─────────────
    @property
    def cancelled(self) -> bool:
        return self.state == CANCELLED

    @property
    def closed(self) -> bool:
        return self.state in TERMINAL

    # ── paths ──────────────────────────────────────────────────────────
    @property
    def _record_path(self) -> Path:
        return _root() / f"{self.job_id}.json"

    @property
    def _log_path(self) -> Path:
        return _root() / f"{self.job_id}.events.jsonl"

    def save(self) -> None:
        try:
            _atomic_write(self._record_path, json.dumps({
                "job_id": self.job_id, "state": self.state, "topic": self.topic,
                "request_key": self.request_key, "error": self.error,
                "last_seq": self.last_seq,
                "created_at": self.created_at.isoformat(),
                "owner_pid": os.getpid(),
            }, indent=1))
        except OSError:
            logging.exception("Could not save the record for job %s", self.job_id)

    # ── writing ────────────────────────────────────────────────────────
    def _append(self, event: ProgressEvent) -> None:
        if self.last_seq >= MAX_EVENTS:
            return                           # a runaway job cannot fill the disk
        self.last_seq += 1
        self.last_event_at = time.monotonic()
        self._events.append((self.last_seq, event))
        del self._events[:-MAX_EVENTS_IN_MEMORY]
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "seq": self.last_seq,
                    "event": json.loads(event.model_dump_json()),
                }) + "\n")
        except OSError:
            logging.exception("Could not log an event for job %s", self.job_id)

    async def _wake(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def publish(self, event: ProgressEvent) -> None:
        """Progress from the pipeline. Ignored once the job has ended: a
        stage that reports after a cancel is reporting on a run that, as
        far as anyone watching is concerned, is over."""
        async with self._changed:
            if self.state in TERMINAL:
                return
            self._append(event)
            self._changed.notify_all()

    async def finish(self, state: str, event: ProgressEvent,
                     error: str | None = None) -> bool:
        """Move to a terminal state and record the event that says so, as
        one step. Returns False, and changes nothing, if the job already
        reached a terminal state: the first ending is the ending."""
        async with self._changed:
            if self.state in TERMINAL:
                return False
            self._append(event)
            self.state, self.error = state, error
            self.finished_at = time.monotonic()
            self.save()
            self._changed.notify_all()
        _forget_running(self)
        return True

    def cancel(self) -> bool:
        """Stop the pipeline. True if it was running and has been told to
        stop.

        Not a coroutine on purpose. The state becomes terminal, and the
        event recording it is logged, before control returns to anything
        else, so there is no moment at which a completing pipeline can
        get in ahead of the cancel."""
        if self.state != RUNNING:
            return False
        live = self.task is not None and not self.task.done()
        self._append(ProgressEvent(
            type="cancelled", stage="cancelled", message="Cancelled by user"))
        self.state, self.error = CANCELLED, "Cancelled by user"
        self.finished_at = time.monotonic()
        self.save()
        _forget_running(self)
        try:
            asyncio.get_running_loop().create_task(self._wake())
        except RuntimeError:
            pass                             # no loop: nobody is waiting
        if live:
            self.task.cancel()
        return live

    # ── reading ────────────────────────────────────────────────────────
    def _from_disk(self, after: int) -> list[tuple[int, ProgressEvent]]:
        found: list[tuple[int, ProgressEvent]] = []
        try:
            with open(self._log_path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                        if entry["seq"] > after:
                            found.append((entry["seq"],
                                          ProgressEvent.model_validate(entry["event"])))
                    except (ValueError, KeyError):
                        continue             # a torn last line, after a crash
        except FileNotFoundError:
            pass
        return found

    def events_after(self, after: int) -> list[tuple[int, ProgressEvent]]:
        oldest_held = self._events[0][0] if self._events else self.last_seq + 1
        if after + 1 >= oldest_held:
            return [(seq, ev) for seq, ev in self._events if seq > after]
        return self._from_disk(after)

    async def subscribe(
        self, after: int = 0, heartbeat: float = HEARTBEAT_SECONDS,
    ) -> AsyncIterator[tuple[int, ProgressEvent] | Heartbeat | Gap]:
        """Everything after `after`, then whatever comes, until the end.

        Holds no queue. A subscriber that stops reading holds nothing on
        the server, and one that disconnects leaves nothing to clean up."""
        cursor = max(0, after)
        while True:
            batch = self.events_after(cursor)
            if batch and batch[0][0] > cursor + 1:
                yield Gap(cursor, batch[0][0])
            for seq, event in batch:
                cursor = seq
                yield seq, event
            async with self._changed:
                if self.last_seq > cursor:
                    continue
                if self.state in TERMINAL:
                    return
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    pass
                else:
                    continue
            yield Heartbeat()


# ── The registry ────────────────────────────────────────────────────────────

_jobs: dict[str, Job] = {}
_running_by_request: dict[str, str] = {}


def request_key(request) -> str:
    """The same request, asked twice, has the same key. Fields that do not
    change what is generated would not belong here; there are none."""
    return hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()[:32]


def running_job_for(key: str) -> Job | None:
    """The job already running for this exact request, if there is one.
    A double click, or a retry from a client that timed out, would
    otherwise start the same paid run twice."""
    job = _jobs.get(_running_by_request.get(key, ""))
    return job if job is not None and job.state == RUNNING else None


def _forget_running(job: Job) -> None:
    if _running_by_request.get(job.request_key) == job.job_id:
        del _running_by_request[job.request_key]
    _evict()


def _evict() -> None:
    finished = sorted(
        (j for j in _jobs.values() if j.state in TERMINAL),
        key=lambda j: j.finished_at or 0)
    for job in finished[:-KEEP_FINISHED_IN_MEMORY or None]:
        _jobs.pop(job.job_id, None)          # still on disk


def create_job(request=None) -> Job:
    key = request_key(request) if request is not None else ""
    job = Job(str(uuid.uuid4()), request_key=key,
              topic=getattr(request, "topic", "") or "")
    _jobs[job.job_id] = job
    if key:
        _running_by_request[key] = job.job_id
    job.save()
    return job


def _load(job_id: str) -> Job | None:
    path = _root() / f"{job_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        job = Job(record["job_id"], request_key=record.get("request_key", ""),
                  topic=record.get("topic", ""))
        job.state = record["state"]
        job.error = record.get("error")
        job.last_seq = int(record.get("last_seq", 0))
        try:
            job.created_at = datetime.fromisoformat(record["created_at"])
        except (KeyError, ValueError):
            pass
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        # Unreadable. Set it aside under a name that says so, rather than
        # failing every request that touches it or deleting the evidence.
        logging.error("Job record %s is unreadable and was set aside", job_id)
        try:
            os.replace(path, path.with_suffix(".json.corrupt"))
        except OSError:
            pass
        return None
    job._on_disk = True
    job.finished_at = 0.0
    if job.state not in TERMINAL:
        # On disk as running, and not held by this process: a restart.
        job.state, job.error = INTERRUPTED, INTERRUPTED_MESSAGE
        held = job._from_disk(0)
        job.last_seq = held[-1][0] if held else 0
        job.last_seq += 1
        event = ProgressEvent(type="error", stage="interrupted", message=INTERRUPTED_MESSAGE)
        try:
            with open(job._log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "seq": job.last_seq,
                    "event": json.loads(event.model_dump_json())}) + "\n")
        except OSError:
            pass
        job.save()
    return job


def get_job(job_id: str) -> Job | None:
    job = _jobs.get(job_id)
    if job is not None:
        return job
    if not job_id or any(ch not in "0123456789abcdef-" for ch in job_id.lower()):
        return None                          # never a path
    return _load(job_id)


def recover() -> dict[str, int]:
    """At startup: mark what a restart cut short, prune old records.
    Starts nothing."""
    root = _root()
    if not root.is_dir():
        return {"interrupted": 0, "pruned": 0}
    interrupted = 0
    records = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in records:
        try:
            state = json.loads(path.read_text(encoding="utf-8")).get("state")
        except (ValueError, OSError):
            continue
        if state not in TERMINAL and path.stem not in _jobs:
            if _load(path.stem) is not None:
                interrupted += 1
    pruned = 0
    for path in records[KEEP_ON_DISK:]:
        for victim in (path, path.with_suffix(".events.jsonl")):
            try:
                victim.unlink()
                pruned += 1
            except OSError:
                pass
    return {"interrupted": interrupted, "pruned": pruned}


def clear_jobs() -> None:
    """Test helper: drop all tracked jobs."""
    _jobs.clear()
    _running_by_request.clear()
