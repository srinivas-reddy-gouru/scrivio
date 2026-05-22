import asyncio
import os

from pipeline.orchestrator.article_workflow import (
    ArticleGenerationWorkflow,
    clarification_activity,
    compilation_activity,
    drafting_activity,
    planning_activity,
    search_activity,
    verification_activity,
    visual_generation_activity,
)


TASK_QUEUE = "article-generation"


async def main() -> None:
    try:
        from temporalio.client import Client
        from temporalio.worker import Worker
    except ModuleNotFoundError as exc:
        raise RuntimeError("temporalio is required to run the worker") from exc

    client = await Client.connect(os.environ["TEMPORAL_HOST"])
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ArticleGenerationWorkflow],
        activities=[
            clarification_activity,
            search_activity,
            planning_activity,
            drafting_activity,
            visual_generation_activity,
            verification_activity,
            compilation_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
