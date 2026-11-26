"""Worker entrypoint: registers the job-watcher workflow and serves it (activities auto-register)."""

import asyncio

import mistralai.workflows as workflows

from workflows.job_watcher import JobWatcherWorkflow


async def main() -> None:
    await workflows.run_worker([JobWatcherWorkflow])


if __name__ == "__main__":
    asyncio.run(main())
