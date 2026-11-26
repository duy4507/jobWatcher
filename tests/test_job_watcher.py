"""Unit tests for the pure job-watcher helpers (no worker or network needed)."""

from datetime import datetime, timezone

from workflows.job_watcher_digest import (
    SEEN_LINE_RE,
    JobPosting,
    JobWatcherInput,
    build_digest,
    parse_postings_json,
)

NOW = datetime(2025, 11, 25, 8, 0, tzinfo=timezone.utc)

AGENT_OUTPUT = """```json
[
  {"id": "https://example.com/job/1", "title": "Data Engineer", "company": "Acme",
   "location": "Paris 11e (hybrid)", "url": "https://example.com/job/1",
   "salary": "55-65k", "posted": "2025-11-24", "match_score": 92,
   "reasons": ["Python/SQL stack match"], "summary": "Build the data platform."},
  {"id": "https://example.com/job/2", "title": "Backend Dev", "company": "Gamma",
   "location": "Lyon on-site", "url": "https://example.com/job/2",
   "salary": null, "posted": null, "match_score": 41,
   "reasons": ["Wrong location"], "summary": "Java backend."}
]
```"""


def _input() -> JobWatcherInput:
    return JobWatcherInput(
        profile="Data engineer, 5y, Python",
        target_roles=["Data Engineer"],
        recipient_email="me@example.com",
    )


def test_parse_postings_json_tolerates_markdown_fences() -> None:
    postings = parse_postings_json(AGENT_OUTPUT)
    assert [p.match_score for p in postings] == [92, 41]
    assert postings[0].company == "Acme"


def test_parse_postings_json_rejects_non_list() -> None:
    try:
        parse_postings_json('{"postings": "nope"}')
    except ValueError:
        return
    raise AssertionError("expected ValueError for a non-list payload")


def test_build_digest_filters_and_marks_seen() -> None:
    data = _input()
    candidates = parse_postings_json(AGENT_OUTPUT)
    top = [p for p in candidates if p.match_score >= data.min_score]
    subject, text, html = build_digest(NOW, data, candidates, top)
    assert "1 new matching job" in subject
    assert "Data Engineer @ Acme" in text
    assert "Backend Dev" not in text
    match = SEEN_LINE_RE.search(text)
    assert match is not None
    assert "https://example.com/job/2" in match.group(1)
    assert "SEEN:" in html


def test_build_digest_empty_digest() -> None:
    subject, text, _ = build_digest(NOW, _input(), [], [])
    assert "no new matching jobs" in subject
    assert "SEEN:" in text


def test_job_posting_round_trip() -> None:
    p = JobPosting(
        id="https://x", title="t", company="c", location="l", url="https://x",
        match_score=80, reasons=["r"], summary="s",
    )
    assert JobPosting.model_validate(p.model_dump()) == p
