"""Pure helpers for the job-watcher workflow: models, agent-output parsing, digest rendering.

No SDK imports here so the module stays importable and unit-testable outside a worker.
"""

import html as html_lib
import json
import re
from datetime import datetime

import pydantic

DIGEST_PREFIX = "[JobWatcher]"
SEEN_LINE_RE = re.compile(r"^SEEN:\s*(.+)$", re.MULTILINE)


class JobPosting(pydantic.BaseModel):
    id: str
    title: str
    company: str
    location: str
    url: str
    salary: str | None = None
    posted: str | None = None
    match_score: int
    reasons: list[str]
    summary: str


class JobWatcherInput(pydantic.BaseModel):
    profile: str
    target_roles: list[str]
    location: str = "Île-de-France, France"
    work_modes: list[str] = ["remote", "hybrid", "on-site"]
    lookback_hours: int = 48
    min_score: int = 70
    max_results: int = 10
    seen_window_days: int = 30
    recipient_email: str
    digest_prefix: str = DIGEST_PREFIX
    notify_when_empty: bool = False
    dry_run: bool = True


class JobWatcherOutput(pydantic.BaseModel):
    dry_run: bool
    candidates_found: int
    new_postings: int
    digest_sent: bool
    digest_subject: str | None = None
    postings: list[JobPosting]


def parse_postings_json(text: str) -> list[JobPosting]:
    """Parse the search agent's final answer into postings, tolerating markdown fences."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    payload = cleaned[start : end + 1] if start != -1 and end > start else cleaned
    data = json.loads(payload)
    if isinstance(data, dict):
        data = data.get("postings") or data.get("jobs") or []
    if not isinstance(data, list):
        raise ValueError(f"agent output is not a list of postings: {text[:200]!r}")
    return [JobPosting.model_validate(item) for item in data]


def seen_line(postings: list[JobPosting]) -> str:
    return "SEEN: " + " | ".join(p.id for p in postings)


def build_digest(
    now: datetime,
    data: JobWatcherInput,
    candidates: list[JobPosting],
    top: list[JobPosting],
) -> tuple[str, str, str]:
    """Render the digest email as (subject, plain-text body, html body).

    The plain-text body ends with a machine-readable SEEN line listing every candidate id,
    which the next run reads back from sent mail to avoid re-sending old postings.
    """
    date_str = now.strftime("%Y-%m-%d")
    if top:
        subject = f"{data.digest_prefix} {len(top)} new matching job{'s' if len(top) != 1 else ''} — {date_str}"
    else:
        subject = f"{data.digest_prefix} no new matching jobs — {date_str}"

    lines = [f"Job digest for {date_str}", ""]
    if top:
        for p in top:
            lines.append(f"{p.match_score}/100 — {p.title} @ {p.company}")
            meta = f"  Location: {p.location}"
            if p.salary:
                meta += f" | Salary: {p.salary}"
            if p.posted:
                meta += f" | Posted: {p.posted}"
            lines.append(meta)
            lines.append(f"  Why it matches: {'; '.join(p.reasons)}")
            lines.append(f"  {p.summary}")
            lines.append(f"  Link: {p.url}")
            lines.append("")
    else:
        lines.append("No new postings above the match threshold since the last digest.")
        lines.append("")
    marker = seen_line(candidates)
    lines.append(marker)
    text_body = "\n".join(lines)

    rows = []
    for p in top:
        meta = html_lib.escape(p.location)
        if p.salary:
            meta += f" | {html_lib.escape(p.salary)}"
        if p.posted:
            meta += f" | Posted {html_lib.escape(p.posted)}"
        reasons = html_lib.escape("; ".join(p.reasons))
        rows.append(
            "<tr>"
            f"<td><b>{p.match_score}/100</b></td>"
            f"<td><a href='{html_lib.escape(p.url, quote=True)}'>{html_lib.escape(p.title)}</a>"
            f" @ {html_lib.escape(p.company)}<br><small>{meta}</small><br>"
            f"{reasons}<br><small>{html_lib.escape(p.summary)}</small></td>"
            "</tr>"
        )
    table = (
        "<table cellspacing='6'>" + "".join(rows) + "</table>"
        if top
        else "<p>No new postings above the match threshold since the last digest.</p>"
    )
    html_body = (
        f"<html><body><h3>Job digest for {date_str}</h3>{table}"
        f"<hr><p style='color:#999;font-size:11px'>{html_lib.escape(marker)}</p>"
        "</body></html>"
    )
    return subject, text_body, html_body
