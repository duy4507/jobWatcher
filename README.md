# jobWatcher

A scheduled Mistral Workflow agent that watches job boards for new postings matching your
profile and emails you a daily digest.

## What it does

Every run (default: daily at 08:00 Europe/Paris, configured as a workflow schedule in
Mistral Studio):

1. `fetch-seen-posting-ids` reads previous `[JobWatcher]` digest emails from Gmail Sent and
   extracts the `SEEN:` marker line, so old postings are never re-sent.
2. `search-job-postings` runs a durable agent with Mistral's built-in `WebSearchTool` over
   French job boards (LinkedIn, Indeed, Welcome to the Jungle, APEC, Les Jeudis, ...) for
   postings published in the last `lookback_hours` (default 48h). Each posting is scored
   0-100 against the candidate profile with reasons, returned as JSON.
3. Postings already seen or below `min_score` (default 70) are filtered out; the top
   `max_results` (default 10) are rendered into an HTML digest.
4. `send-digest-email` sends the digest through the `gmail` connector — or, with
   `dry_run: true` (the default), saves it as a Gmail draft instead of sending.

## Layout

- `worker.py` — worker entrypoint (`workflows.run_worker`).
- `workflows/job_watcher.py` — the `job-watcher` workflow + activities (Gmail connector,
  web-search agent).
- `workflows/job_watcher_digest.py` — pure helpers: input/output models, agent-JSON
  parsing, digest rendering, `SEEN:` dedup marker.
- `tests/test_job_watcher.py` — unit tests for the pure helpers (`python -m pytest`).
- `Dockerfile` — image for the managed deployment; the platform injects
  `DEPLOYMENT_NAME`, `SERVER_URL` and the service-account token.

## Configuration

The schedule passes a JSON input, e.g.:

```json
{
  "profile": "Data engineer, 5 years, Python/SQL/dbt, ...",
  "target_roles": ["Data Engineer", "Analytics Engineer"],
  "location": "Île-de-France, France",
  "work_modes": ["remote", "hybrid"],
  "lookback_hours": 48,
  "min_score": 70,
  "max_results": 10,
  "recipient_email": "you@example.com",
  "dry_run": true
}
```

## Notes

- Scheduled runs have no user session, so the `gmail` connector needs a
  **workspace-scope credential** in Mistral Studio (Connectors), not only the personal
  OAuth one.
- `dry_run: true` creates a draft instead of sending, for safe first runs.
