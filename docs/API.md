# Local HTTP API

Start with `intelpod serve` (default `http://127.0.0.1:8000`, bound to localhost only).
Interactive OpenAPI docs are served at `/docs`. All responses are JSON; times are UTC ISO-8601.
No endpoint returns secrets.

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | status, version, schema version, article count, whether a run is in progress, last background error |
| GET | `/api/runs?limit=30` | recent runs with status and error |
| POST | `/api/runs?resume=false&run_id=` | start a run in the background (202); 409 if one is already running |
| GET | `/api/runs/{run_id}` | run params and per-stage status, attempts, errors and output summaries |
| GET | `/api/runs/{run_id}/events?selected_only=true` | ranked events of a run (full scores and evidence) |
| GET | `/api/events/latest?selected_only=true` | ranked events of the latest completed run |
| GET | `/api/events/{event_id}` | event details, all linked articles, assessment history across runs |
| GET | `/api/feeds` | feed registry merged with health (last status, items, failures) |
| GET | `/api/usage` | requests per provider in the last 24 h |

## RankedEvent (abridged)

```json
{
  "rank": 1, "selected": true, "event_id": "evt_20261016_885bcf8e",
  "title": "…", "description": "…", "topic": "semiconductors_minerals", "subtopic": "chip",
  "countries": ["United States", "China"], "publishers": ["…"],
  "independent_sources": 4, "article_count": 5, "importance": 50.4,
  "components": [{"name": "business", "value": 0.79, "weight": 0.8,
                  "evidence": ["terms: semiconductor, export controls", "5/5 reports on …"]}],
  "penalties": [], "flags": ["some_dates_approximate"],
  "rationale": "Selected #1 with importance 50.4/100. Main drivers: …",
  "first_article_at": "…", "latest_article_at": "…", "cluster_confidence": 0.716, "is_continuing": false
}
```

## Dashboard pages

`/` briefing · `/events/{id}` event detail · `/runs` execution status, failures, request counts,
start/resume buttons · `/feeds` source health · `/settings` current configuration (read-only in Phase 1).
