"""Daily job-posting watcher.

Runs on a schedule (no triggering user, so not on_behalf_of and no connector slots):
a durable agent with web search finds postings posted in the last `lookback_hours` that
match the profile, filters out ids already reported in earlier digest emails, and sends
an HTML digest through the Gmail REST API. Gmail auth uses an OAuth refresh token from
the worker's environment (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN,
bound from workspace secrets at deploy time), because the gmail connector is OAuth-only
and cannot resolve credentials for unattended runs. `dry_run=True` creates a draft
instead of sending.
"""

import base64
import os
from datetime import timedelta
from email.message import EmailMessage

import requests
import mistralai.client.models
import mistralai.workflows as workflows
import mistralai.workflows.plugins.mistralai as workflows_mistralai

from workflows.job_watcher_digest import (
    SEEN_LINE_RE,
    JobPosting,
    JobWatcherInput,
    JobWatcherOutput,
    build_digest,
    parse_postings_json,
)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

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


def _gmail_access_token() -> str:
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _gmail_get(path: str, params: dict | None = None) -> dict:
    response = requests.get(
        f"{GMAIL_API}/{path}",
        params=params,
        headers={"Authorization": f"Bearer {_gmail_access_token()}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _gmail_post(path: str, payload: dict) -> dict:
    response = requests.post(
        f"{GMAIL_API}/{path}",
        json=payload,
        headers={"Authorization": f"Bearer {_gmail_access_token()}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _message_text(message: dict) -> str:
    payload = message.get("payload") or {}
    parts = payload.get("parts") or []
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        parts = [payload]
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
    return message.get("snippet") or ""


def _raw_mime(to: str, subject: str, text_body: str, html_body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = "me"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


@workflows.activity(
    name="fetch-seen-posting-ids",
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy_max_attempts=3,
)
async def fetch_seen_posting_ids(
    digest_prefix: str,
    seen_window_days: int,
) -> list[str]:
    """Read SEEN lines from previously sent digest emails to deduplicate postings."""
    seen: set[str] = set()
    page_token: str | None = None
    for _ in range(5):
        params: dict = {
            "q": f'in:sent subject:"{digest_prefix}" newer_than:{seen_window_days}d',
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        result = _gmail_get("threads", params)
        for thread in result.get("threads") or []:
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not thread_id:
                continue
            thread_data = _gmail_get(f"threads/{thread_id}")
            for message in thread_data.get("messages") or []:
                match = SEEN_LINE_RE.search(_message_text(message))
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
) -> dict:
    return _gmail_post("messages/send", {"raw": _raw_mime(to, subject, text_body, html_body)})


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
) -> dict:
    return _gmail_post(
        "drafts/create", {"message": {"raw": _raw_mime(to, subject, text_body, html_body)}}
    )


@workflows.workflow.define(name="job-watcher")
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
