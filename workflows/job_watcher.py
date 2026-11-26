"""Daily job-posting watcher.

Runs on a schedule (no triggering user, so not on_behalf_of): a durable agent with web
search finds postings posted in the last `lookback_hours` that match the profile, filters
out ids already reported in earlier digest emails, and sends an HTML digest through the
gmail connector. `dry_run=True` creates a draft instead of sending.
"""

from datetime import timedelta

import mistralai.client.models
import mistralai.workflows as workflows
import mistralai.workflows.plugins.mistralai as workflows_mistralai
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

from workflows.job_watcher_digest import (
    SEEN_LINE_RE,
    JobPosting,
    JobWatcherInput,
    JobWatcherOutput,
    build_digest,
    parse_postings_json,
)

gmail = connector("gmail")

SEARCH_INSTRUCTIONS = """You are a job-search agent for a candidate in France.

Use web search to find REAL, currently open job postings. Search multiple queries and
multiple sources, for example: site:linkedin.com/jobs, site:indeed.fr, site:welcometothejungle.com,
site:apec.fr, site:lesjeudis.com, site:cadremploi.fr, site:hellowork.com, plus general queries
like "<role> <location> CDI posted last 48 hours". Prefer postings published within the last
{lookback_hours} hours; today is {today}.

Candidate profile:
{profile}

Target roles: {target_roles}
Location: {location}
Accepted work modes: {work_modes}

For each distinct posting (deduplicate by URL), decide a match_score from 0 to 100 against the
profile (skills, seniority, role, location/work mode, deal-breakers if stated). Only include
postings you verified via search results; never invent postings. Collect at most 25 postings.

Your FINAL message must be ONLY a JSON array (no prose, no markdown fences), one object per posting:
[{{"id": "<canonical url, use as id>", "title": "...", "company": "...", "location": "...",
"url": "<application or posting url>", "salary": "<or null>", "posted": "<date if known or null>",
"match_score": <int 0-100>, "reasons": ["..."], "summary": "<one sentence>"}}]
"""


def _dump(result: object) -> dict:
    if hasattr(result, "model_dump"):
        return result.model_dump()  # type: ignore[union-attr]
    if isinstance(result, dict):
        return result
    return {"raw": str(result)}


@workflows.activity(
    name="fetch-seen-posting-ids",
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=3,
)
async def fetch_seen_posting_ids(
    digest_prefix: str,
    seen_window_days: int,
    client: ToolCallClient = Depends(gmail),
) -> list[str]:
    """Read SEEN lines from previously sent digest emails to deduplicate postings."""
    seen: set[str] = set()
    page_token: str | None = None
    for _ in range(5):
        arguments: dict = {
            "query": f'in:sent subject:"{digest_prefix}" newer_than:{seen_window_days}d',
            "pageSize": 50,
        }
        if page_token:
            arguments["pageToken"] = page_token
        result = _dump(await client.call_tool(tool_name="search_threads", arguments=arguments))
        for thread in result.get("threads") or []:
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not thread_id:
                continue
            thread_data = _dump(
                await client.call_tool(tool_name="get_thread", arguments={"threadId": thread_id})
            )
            for message in thread_data.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                body = message.get("plaintextBody") or message.get("snippet") or ""
                match = SEEN_LINE_RE.search(body)
                if match:
                    seen.update(
                        item.strip().strip(".,;")
                        for item in match.group(1).split("|")
                        if item.strip()
                    )
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return sorted(seen)


@workflows.activity(
    name="search-job-postings",
    start_to_close_timeout=timedelta(minutes=15),
    retry_policy_max_attempts=2,
)
async def search_job_postings(
    profile: str,
    target_roles: list[str],
    location: str,
    work_modes: list[str],
    lookback_hours: int,
    today: str,
) -> list[JobPosting]:
    instructions = SEARCH_INSTRUCTIONS.format(
        lookback_hours=lookback_hours,
        today=today,
        profile=profile,
        target_roles=", ".join(target_roles),
        location=location,
        work_modes=", ".join(work_modes),
    )
    agent = workflows_mistralai.Agent(
        model="mistral-large-latest",
        name="job-search-agent",
        description="Finds and scores new job postings matching a candidate profile.",
        instructions=instructions,
        tools=[mistralai.client.models.WebSearchTool()],
    )
    outputs = await workflows_mistralai.Runner.run(
        agent=agent,
        inputs="Find and score the latest matching job postings now. Return only the JSON array.",
        session=workflows_mistralai.RemoteSession(),
        max_turns=25,
    )
    text = "\n".join(
        chunk.text
        for chunk in outputs
        if isinstance(chunk, mistralai.client.models.TextChunk)
    )
    return parse_postings_json(text)


@workflows.activity(
    name="send-digest-email",
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=1,
)
async def send_digest_email(
    to: str,
    subject: str,
    text_body: str,
    html_body: str,
    client: ToolCallClient = Depends(gmail),
) -> dict:
    return _dump(
        await client.call_tool(
            tool_name="send_email",
            arguments={"to": [to], "subject": subject, "body": text_body, "htmlBody": html_body},
        )
    )


@workflows.activity(
    name="create-digest-draft",
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=3,
)
async def create_digest_draft(
    to: str,
    subject: str,
    text_body: str,
    html_body: str,
    client: ToolCallClient = Depends(gmail),
) -> dict:
    return _dump(
        await client.call_tool(
            tool_name="create_draft",
            arguments={"to": [to], "subject": subject, "body": text_body, "htmlBody": html_body},
        )
    )


@workflows.workflow.define(name="job-watcher")
@uses_connectors(gmail)
class JobWatcherWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, data: JobWatcherInput) -> JobWatcherOutput:
        now = workflows.workflow.now()
        seen_ids = await fetch_seen_posting_ids(data.digest_prefix, data.seen_window_days)
        candidates = await search_job_postings(
            profile=data.profile,
            target_roles=data.target_roles,
            location=data.location,
            work_modes=data.work_modes,
            lookback_hours=data.lookback_hours,
            today=now.isoformat(),
        )
        seen = set(seen_ids)
        fresh = [
            p for p in candidates if p.id not in seen and p.match_score >= data.min_score
        ]
        fresh.sort(key=lambda p: p.match_score, reverse=True)
        top = fresh[: data.max_results]

        subject, text_body, html_body = build_digest(now, data, candidates, top)
        sent = False
        if top or data.notify_when_empty:
            if data.dry_run:
                await create_digest_draft(data.recipient_email, subject, text_body, html_body)
            else:
                await send_digest_email(data.recipient_email, subject, text_body, html_body)
                sent = True
        return JobWatcherOutput(
            dry_run=data.dry_run,
            candidates_found=len(candidates),
            new_postings=len(fresh),
            digest_sent=sent,
            digest_subject=subject if (top or data.notify_when_empty) else None,
            postings=top,
        )
