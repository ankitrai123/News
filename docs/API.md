# Local HTTP API

Start with `intelpod serve` (default `http://127.0.0.1:8000`, bound to localhost only).
Requests must use a local Host name (127.0.0.1 / localhost; add others with `--allow-host`), and
POST requests from other websites (cross-origin `Origin`/`Referer` or `Sec-Fetch-Site`) are
refused with 403, because starting a run spends NewsData credits and NVIDIA requests.
Interactive OpenAPI docs are served at `/docs`. All responses are JSON; times are UTC ISO-8601.
No endpoint returns secrets.

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | status, version, schema version, article count, whether a run is in progress, last background error |
| GET | `/api/runs?limit=30` | recent runs with status and error (`limit` 1–500, else 422) |
| POST | `/api/runs?resume=false&run_id=` | start a run in the background (202). 409 if a run is in progress (in the dashboard or any other process). `resume=true` continues the latest unfinished run, or `run_id`; 409 "Nothing to resume" if the latest run completed, 404 for an unknown `run_id`, 422 for `run_id` without `resume` or a malformed id |
| GET | `/api/runs/{run_id}` | run params and per-stage status, attempts, errors and output summaries |
| GET | `/api/runs/{run_id}/events?selected_only=true` | ranked events of a run (full scores and evidence) |
| GET | `/api/events/latest?selected_only=true` | ranked events of the latest completed run |
| GET | `/api/events/{event_id}` | event details, all linked articles, assessment history across runs |
| GET | `/api/runs/{run_id}/research` | per selected event: package status, number of sources, coverage per research question, content mix, limitations |
| GET | `/api/events/{event_id}/research?run=&all_sources=false` | the research package (latest, or for `run`) and its selected sources (`all_sources=true`: also those not used, with reasons); 404 if none |
| GET | `/api/feeds` | feed registry merged with health (last status, items, failures) |
| GET | `/api/usage` | requests per provider (24 h), model calls/tokens per model (24 h), NewsData requests today vs budget |

## RankedEvent (abridged)

```json
{
  "rank": 1, "selected": true, "event_id": "evt_20261016_885bcf8e",
  "title": "…", "description": "…", "topic": "semiconductors_minerals", "subtopic": "chip",
  "countries": ["United States", "China"], "publishers": ["…"],
  "independent_sources": 4, "article_count": 5, "importance": 50.4,
  "components": [{"name": "business", "value": 0.79, "weight": 0.8,
                  "evidence": ["terms: semiconductor, export controls", "5/5 reports on …"]}],
  "penalties": [], "flags": ["dates_approximate"],
  "rationale": "Selected #1 with importance 50.4/100. Main drivers: …",
  "headline": "publisher headline when the title was written by the model, else null",
  "summary": "model summary that passed the grounding check, else null",
  "review": {"provider": "nvidia", "model": "…", "outlier_article_ids": [], "rejected": {},
             "soft_news": false, "scores": {"economic": {"score": 0.8, "reason": "…"}}},
  "first_article_at": "…", "latest_article_at": "…", "cluster_confidence": 0.716, "is_continuing": false
}
```

## Dashboard pages

`/` briefing · `/events/{id}` event detail · `/runs` execution status, failures, request counts,
start/resume buttons · `/feeds` source health · `/settings` current configuration (read-only in Phase 1).
