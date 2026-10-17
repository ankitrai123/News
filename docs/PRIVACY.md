# Privacy, data retention and copyright

IntelPod is a single-user local application. It has no telemetry, accounts or cloud component.

## What it stores (in `data/`)

| Data | Where | Content |
|---|---|---|
| Article metadata | `articles` table | title, canonical URL, publisher, timestamps, language, country, feed categories, author name when the feed gives one, and the feed's short summary truncated to 400 characters |
| Events and scores | `events`, `event_articles`, `event_scores` | IntelPod's own clustering and analysis |
| Run history and request log | `pipeline_runs`, `stage_runs`, `fetch_log` | timings, counts, error messages |
| Feed cache validators | `feed_state` | ETag / Last-Modified headers |
| Reports | `data/runs/<run_id>/` | Markdown/JSON reports linking to the original articles |
| Logs | `data/logs/` | rotating, 3 × 5 MB; API keys and `token=`/`apikey=` parameters are redacted |

Full article text is **not** fetched or stored in Phase 1 (`content_access = "metadata"`).
Later phases will store short factual extracts and IntelPod's own notes, not full articles.

## What it sends

HTTP requests to the configured feeds and GDELT, identified by your `user_agent`. Publishers see
your IP address, as with any browser visit. Ollama runs on your machine; nothing is sent to a
model provider.

## Retention

Nothing is deleted automatically. The database grows by roughly a few MB per month at default
settings. To remove everything, delete `data/`. To trim old raw articles while keeping events,
run for example:

```bash
sqlite3 data/intelpod.db "DELETE FROM articles WHERE discovered_at < date('now','-180 days')
  AND article_id NOT IN (SELECT article_id FROM event_articles);"
```

## Copyright

Headlines and summaries belong to their publishers. IntelPod links to the original reporting,
respects `robots.txt`, does not bypass paywalls, logins or CAPTCHAs, and is intended for personal
use. If you publish generated output, review the relevant publishers' terms first.
