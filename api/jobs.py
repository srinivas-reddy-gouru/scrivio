import asyncio
import uuid
from datetime import datetime, timezone

from pipeline.schemas.models import ProgressEvent, PublishedArticle


class Job:
    """In-process job state: a progress queue plus the eventual result.

    The queue holds ProgressEvent objects. A None sentinel signals the stream
    is finished and the SSE endpoint should close.

    `task` holds the asyncio.Task running the pipeline. Storing it here is
    what makes cancellation possible — without the handle, there's no way
    to stop work that's already in flight.

    `cancelled` is a sticky flag set by `cancel()` so that the SSE stream
    handler and the /jobs/{id} status endpoint can report the cancellation
    even after `task.cancel()` has propagated.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
        self.result: dict[str, PublishedArticle] | None = None
        self.error: str | None = None
        self.cancelled: bool = False
        self.task: asyncio.Task | None = None
        self.created_at = datetime.now(timezone.utc)

    async def publish(self, event: ProgressEvent) -> None:
        await self.queue.put(event)

    async def close(self) -> None:
        await self.queue.put(None)

    def cancel(self) -> bool:
        """Stop the in-flight pipeline. Returns True if the task was running
        and has now been signaled to cancel, False if it had already
        finished or was never started."""
        self.cancelled = True
        if self.task is None or self.task.done():
            return False
        return self.task.cancel()


_jobs: dict[str, Job] = {}


def create_job() -> Job:
    job_id = str(uuid.uuid4())
    job = Job(job_id)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def clear_jobs() -> None:
    """Test helper: drop all tracked jobs."""
    _jobs.clear()
