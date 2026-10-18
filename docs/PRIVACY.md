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
| Research (Phase 2) | `research_items`, `research_queries`, `research_results`, `research_packages` | research-feed item metadata; the searches run; for each assessed source: title, URL, publisher, dates, scores, and at most 3 verbatim extracts of ≤ 300 characters plus up to 3 attributed sentences and IntelPod/model notes |
| Debate (Phase 3) | `debate_packages`, `claims` | claims: the same short verbatim statements already stored with research results (headlines, extracts, attributed sentences), with source ids; model-written position summaries checked against the cited sources; no new page text is stored |
| Impact (Phase 4) | `impact_analyses`, `event_links`, `forecasts`, `intelligence_briefs` | references to stored claims, short quotes already stored, checked model-written chains/summaries, forecast ledger entries (verbatim forecast, source, date, your recorded outcome) |
| Research caches | `search_cache`, `page_cache` | search answers (metadata) for 12 h; page metadata and fetch outcome (never page text) for 7 days |
| Reports | `data/runs/<run_id>/` | Markdown/JSON reports linking to the original articles; `research.json`, `debate.json`, `intelligence_brief.{md,json}` |
| Logs | `data/logs/` | rotating, 3 × 5 MB; API keys and `token=`/`apikey=` parameters are redacted |

Phase 1 never fetches article pages. Phase 2 fetches result pages only where robots.txt allows,
never behind paywalls or logins, and keeps the page text in memory only for the current run: it
stores short verbatim extracts (see above) and IntelPod's notes, never full articles. Pages that
opt out (`noai`, `noarchive`, `nosnippet`, TDM reservation) or are paywalled contribute metadata only.

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
* **Research (Phase 2):** search terms derived from the event go to GDELT and, if
  `GUARDIAN_API_KEY` is set, to the Guardian Content API (key in the query string, never logged);
  result pages are requested with your `user_agent`. With a model configured, research also sends
  event headlines and the short extracts (never full pages) to NVIDIA/Ollama for keywords and notes.
* **Debate (Phase 3):** viewpoint searches send event keywords (plus cue words or a country filter)
  to the same providers; with a model configured, the numbered sources (publisher, country, date,
  title and the stored short extracts) are sent to NVIDIA/Ollama for the debate analysis.
* **Impact (Phase 4):** with a model configured, the event title and its numbered statements
  (verbatim claims with publisher names) are sent to NVIDIA/Ollama; no new network requests to
  publishers are made.
* **Ollama** runs on your machine; nothing leaves it.

API keys live only in your environment or `.env` (git-ignored). They are never written to the
database, reports or logs (log lines are redacted; NewsData keys travel in a header, not the URL).

## Retention

Research-feed items are pruned after `research.feeds.keep_days` (45), the search cache after 2 days
and page metadata after `research.pages.cache_days` (7). Besides those, only the embedding cache is pruned automatically (entries older than 14 days; it stays below
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
