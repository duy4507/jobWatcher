"""Worker entrypoint: registers the job-watcher workflow and its activities and serves them."""

import asyncio

import mistralai.workflows as workflows

from workflows.job_watcher import (
    JobWatcherWorkflow,
    create_digest_draft,
    fetch_seen_posting_ids,
    search_job_postings,
    send_digest_email,
)


async def main() -> None:
    await workflows.run_worker(
        [
            JobWatcherWorkflow,
            fetch_seen_posting_ids,
            search_job_postings,
            send_digest_email,
            create_digest_draft,
        ]
    )


if __name__ == "__main__":
    asyncio.run(main())
