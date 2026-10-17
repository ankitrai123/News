# Privacy, data retention and copyright

IntelPod is a single-user local application. It has no telemetry, accounts or cloud component.

## What it stores (in `data/`)

| Data | Where | Content |
|---|---|---|
| Article metadata | `articles` table | title, canonical URL, publisher, timestamps, language, country, feed categories, author name when the feed gives one, and the feed's short summary truncated to 400 characters |
| Events and scores | `events`, `event_articles`, `event_scores` | IntelPod's own clustering and analysis |
| Run history and request log | `pipeline_runs`, `stage_runs`, `fetch_log` | timings, counts, error messages |
| Feed cache validators | `feed_state` | ETag / Last-Modified headers |
| Model calls | `llm_calls` | provider, model, task, tokens, latency, errors (no prompt text) |
| Model answer cache | `llm_cache` | model answers keyed by a hash of the request |
| Embedding cache | `embedding_cache` | vectors keyed by a hash of model + text; pruned after 14 days |
| Reports | `data/runs/<run_id>/` | Markdown/JSON reports linking to the original articles |
| Logs | `data/logs/` | rotating, 3 × 5 MB; API keys and `token=`/`apikey=` parameters are redacted |

Full article text is **not** fetched or stored in Phase 1 (`content_access = "metadata"`).
Later phases will store short factual extracts and IntelPod's own notes, not full articles.

## What it sends, and to whom

* **Feeds and GDELT:** plain HTTP requests identified by your `user_agent` (which may include the
  contact you configured); publishers see your IP address, as with any browser visit.
* **NewsData.io** (if `NEWSDATA_API_KEY` is set): your search queries and your key (in a header).
  API requests carry only the User-Agent `IntelPod/<version>`, never your contact details.
* **NVIDIA** (if `NVIDIA_API_KEY` is set): article headlines and the feeds' short summaries, for
  embeddings and for the event review. Under NVIDIA's trial terms requests are logged for abuse
  monitoring and NVIDIA may use prompts and outputs to improve its services. IntelPod sends only
  public news text, never personal data. `--no-llm` (or `llm.enabled: false`) stops all NVIDIA
  traffic: no model review and no NVIDIA embeddings. To keep model features but stay local, set
  both `llm.provider: ollama` and `clustering.embeddings.provider: ollama`.
* **Ollama** runs on your machine; nothing leaves it.

API keys live only in your environment or `.env` (git-ignored). They are never written to the
database, reports or logs (log lines are redacted; NewsData keys travel in a header, not the URL).

## Retention

Only the embedding cache is pruned automatically (entries older than 14 days; it stays below
~100 MB at default settings). Everything else is kept; articles and events grow by a few MB per month. To remove everything, delete `data/`. To trim old raw articles while keeping events,
run for example:

```bash
sqlite3 data/intelpod.db "DELETE FROM articles WHERE discovered_at < date('now','-180 days')
  AND article_id NOT IN (SELECT article_id FROM event_articles);"
```

## Copyright

Headlines and summaries belong to their publishers. IntelPod links to the original reporting,
respects `robots.txt`, does not bypass paywalls, logins or CAPTCHAs, and is intended for personal
use. If you publish generated output, review the relevant publishers' terms first.
