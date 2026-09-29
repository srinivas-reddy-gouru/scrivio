"""The Temporal worker for article generation.

Optional, and separate from the web application: articles started from
the interface run in the API process and do not come through here. See
"Temporal" in the README for what is and is not supported.
"""
import asyncio
import os

from pipeline.orchestrator import article_workflow
from pipeline.orchestrator.article_workflow import ArticleGenerationWorkflow

TASK_QUEUE = "article-generation"


def all_activities() -> list:
    """Every activity defined beside the workflow, found rather than
    listed. The list used to be written out by hand and had fallen five
    behind: brief, gap fill, editor, revision, and humanization were
    scheduled by the workflow and registered by nobody, so a real run
    would have stopped at the first of them."""
    found = [
        value for name, value in vars(article_workflow).items()
        if name.endswith("_activity") and callable(value)
        and getattr(value, "__module__", "") == article_workflow.__name__
    ]
    return sorted(found, key=lambda fn: fn.__name__)


async def main() -> None:
    try:
        from temporalio.client import Client
        from temporalio.worker import Worker
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "temporalio is required to run the worker: "
            "pip install -r requirements-temporal.txt -c constraints.txt") from exc

    client = await Client.connect(os.environ["TEMPORAL_HOST"])
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ArticleGenerationWorkflow],
        activities=all_activities(),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
