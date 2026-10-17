# Architecture

## Principles

1. **Zero recurring cost.** Keyless public sources, open-source libraries, SQLite, local models.
2. **Deterministic before expensive.** Normalization, de-duplication, clustering and ranking
   run without any model. LLMs (Phase 2+) are used only where rules cannot do the job.
3. **Every stage is a module with typed input/output** (`intelpod/schemas.py`), persisted so a
   failed run resumes where it stopped.
4. **Degrade, don't fabricate.** A dead feed is logged and skipped; if every source fails the
   run stops with a clear error. Nothing is ever filled in with invented content.
5. **Smallest system that works.** One Python process, one SQLite file, server-rendered pages.
   No queue, no microservices, no JS build step.

## Layout

```
intelpod/
  config.py            settings: defaults < YAML < INTELPOD_* env vars
  schemas.py           pydantic contracts: RawItem, Article, EventCandidate, RankedEvent
  db.py                SQLAlchemy models, SQLite pragmas (WAL, FK), schema version
  repository.py        all queries used by stages, CLI and web
  http.py              polite client: timeouts, retries/backoff, Retry-After, per-host
                       throttling, robots.txt
  textutil.py          URL canonicalization, title normalization, tokenizer/stemmer,
                       taxonomy matcher, country gazetteer, proper-noun extraction
  logging_setup.py     console + rotating file logs, optional JSON, secret redaction
  providers/
    base.py            NewsProvider interface, FetchRecord, ProviderResult
    rss.py             RSS/Atom (feedparser) with conditional GET
    gdelt.py           GDELT DOC 2.0 ArtList
    newsdata.py        NewsData.io /latest (key in header, credit budgeting, 429 variants)
  llm/
    client.py          OpenAI-compatible chat client (NVIDIA / Ollama): model failover,
                       pacing, retries, 202 polling, cache, usage log, JSON validation + repair
    parsing.py         JSON extraction, reasoning stripping, grounding check
  pipeline/
    normalize.py       RawItem -> Article
    dedup.py           URL / title / syndication de-duplication
    cluster.py         article -> event clustering, linking to earlier events
    embeddings.py      NVIDIA / Ollama embedders + SQLite embedding cache
    rank.py            importance scoring, selection, explanations, model-score blending
    review.py          model review of top events: prompt, validation, grounding, outliers
    report.py          Markdown + JSON output
    runner.py          stage orchestration, persistence, resume, retries
  web/app.py           FastAPI JSON API + Jinja2 dashboard
  cli.py               `intelpod` command
config/                config.example.yaml, feeds.yaml, taxonomy.yaml
tests/                 offline tests + fixtures; test_live.py for real network
```

## Stages (Phase 1)

| Stage | Input | Output (persisted in `stage_runs.output`) | Failure behaviour |
|---|---|---|---|
| `ingest` | provider config | per-provider request/item counts, dedup stats, inserted count, warnings | single source failures are logged and skipped; if **all** requests fail and no stored articles are in the window → `StageError` (run fails, resumable); if all fail but older articles exist → continues with a "stale data" warning |
| `cluster` | articles in `[now - lookback, now]` (English) | `EventCandidate` list; events and event–article links written to DB | no articles → `StageError` |
| `rank` | candidates from `cluster` output | `RankedEvent` list in `event_scores` (with full explanation) | retried once |
| `review` | top-ranked events (default 12) | per-event model reviews; outliers unlinked; scores re-ranked and overwritten in `event_scores`; reviewed candidates | **never fails the run**: no key / disabled → `skipped`; provider rejects key, rate limit persists, or every configured model is retired → remaining reviews stop and keyword ranking stands (`failed_all` if none succeeded); one bad answer only affects that event |
| `report` | ranked events | `data/runs/<run_id>/phase1_events.{md,json}` | retried once |

Runs record their `now` timestamp, so a resumed run uses the identical time window.
A run in progress blocks a second run (`--force` overrides a stale lock).

## De-duplication

1. **Canonical URL** — https, lowercase host without `www.`/`amp.`, no fragment, tracking
   parameters removed (`utm_*`, `fbclid`, `gclid`, …), sorted query, no trailing slash or
   `/amp`. `article_id = sha1(canonical_url)[:16]`.
2. **Same normalized title from the same publisher** within 4 days → duplicate.
3. **Syndication groups** — identical or near-identical (RapidFuzz token-set ≥ 92) titles from
   *different* publishers are kept but share a `syndication_group`, so a wire story printed by
   ten outlets counts as one independent source.

## Clustering

* Tokens: title (×2) + description → phrase folding (`electric vehicles→ev`,
  `export controls→exportcontrol`), lowercase, hyphens split, stopwords removed, synonym folding
  from `taxonomy.yaml` (`semiconductor→chip`, `curbs→restrict`, …), light stemming. Country words
  are removed from the lexical vector because countries are compared as entities.
* TF-IDF vectors over the batch; cosine similarity.
* Entities: canonical countries (Washington/American/White House → United States) and
  capitalised proper-noun phrases.
* `similarity = cosine + entity_weight × entity_overlap × gate`, where `gate = min(1, cosine/0.2)`.
  Shared entities are a bonus scaled by lexical agreement, so two unrelated stories that both
  mention "United States" do not merge.
* Optional embeddings (NVIDIA `nemotron-3-embed-1b` by default, or Ollama), cached per text.
  Embedding models have very different cosine "floors" (unrelated headlines often score 0.6–0.8),
  so raw embedding cosine is **calibrated**: the 90th percentile of sampled pairs in the batch
  maps to 0. Clusters compare against the *average* member embedding similarity, not a dense
  centroid, which would attract everything. Default mode `boost` lets embeddings only **raise**
  similarity (catching paraphrases), never lower it: on the stress test, `blend` mode split real
  events when embeddings were uninformative (completeness 0.996 → 0.866), while `boost` never
  did worse than lexical-only. Without calibration a high-floor model merged every article into
  one cluster; a regression test guards this.
* Greedy pass in time order (articles more than 72h apart never merge). An article joins the
  best cluster above `similarity_threshold` **only if it also directly matches at least one
  member** (`member_min_similarity`). Without this guard a growing cluster's averaged centroid
  matches almost anything; on a planted-event stress test (`tests/test_cluster_quality.py`) the
  guard raised purity from 0.76 to 0.90 with completeness unchanged at 0.99.
* Iterative centroid merging with the same direct-match guard.
* **Event identity across runs:** a cluster that shares articles with, or closely matches the
  centroid of, an event seen in the last 7 days reuses that `event_id` (one-to-one). The event
  row keeps first-seen and last-development timestamps.
* `cluster_confidence` = mean member-to-centroid cosine (null for single-article events).

## Ranking

Eight components, each in [0, 1] with recorded evidence:

| Component | Computed from |
|---|---|
| geopolitical | share of reports on geopolitics/defence/elections, high-impact terms (sanctions, invasion, export controls…), number of countries involved; figurative phrases like "trade war" are ignored |
| economic | share of reports on macro/trade/energy/markets, high-impact terms (inflation, rate cut, tariff…) |
| business | share on corporate/tech/semiconductors/supply chains/trade, business terms |
| india | share of reports mentioning India terms, distinct India terms, India among the event's countries |
| novelty | 1.0 for a new event; 0.3 + 0.7 × share of new reports for continuing stories |
| long_term | structural/policy terms (law, treaty, regulation, export controls…), primary-source presence |
| corroboration | `log(1 + independent sources) / log(1 + cap)`; syndicated copies collapse to one; official sources guarantee ≥ 0.5 |
| uncertainty | risk/uncertainty language × the event's strongest significance score |

`importance = 100 × Σ(weight × value) / Σ weight`, then multiplied by penalties: soft news
(×0.3, and never selected) and single-source (×0.85). Selection: highest first while
`importance ≥ min_score`, at most `max_per_topic` per topic and `max_events` in total; if fewer
than `min_events` qualify, events above `quiet_day_floor` fill in, flagged `quiet_day_fill`.

**Model review blending.** For reviewed events, each of geopolitical, economic, business, India
and long-term becomes `(1 − w) × keyword + w × model` (`llm.event_review.weight`, default 0.5);
the evidence shows both numbers and the model's one-line reason. A model "soft news" judgement
applies the soft-news penalty. Novelty, corroboration and uncertainty stay rule-based.

These are heuristics over headlines and short descriptions. They are transparent and tunable,
not a judgement of truth.

## Model review safeguards

* The model sees only the retrieved reports (publisher, date, headline, feed summary) with short
  ids (`r1`…); the system prompt tells it to treat report text as data. Unknown ids are ignored;
  if it rejects every report, the cluster is kept as is.
* The answer must validate against a pydantic schema with five explicit score fields (requested
  via `response_format: json_schema`); invalid JSON gets one repair round-trip.
* **Grounding check:** a model title or summary is rejected if it contains any number or
  capitalised name that appears in none of the reports; the publisher headline is kept instead.
  Rejections are recorded and shown.
* Outliers are unlinked from the event (they remain stored as articles).
* Every call is logged (`llm_calls`: model, tokens, latency, error) and cached (`llm_cache`);
  only complete answers (`finish_reason: stop`) are cached.

## LLM client behaviour (NVIDIA specifics from NVIDIA's client source and forum staff)

| Situation | Handling |
|---|---|
| No key / disabled | stage `skipped`, reason recorded |
| 401/402/403 | provider unusable for this run; 403 message explains the "Public API Endpoints" entitlement |
| 404 "not found for account" / 410 Gone | that model is skipped for the run; next model in `llm.tasks.<task>.models` |
| 422 "body -> field … extra_forbidden" | field dropped for that model, retried once |
| 400/422 on structured output | retried with prompt-only JSON for that model |
| 429 (no Retry-After) | backoff 5 → 10 → 20 s, then stop calling for this run |
| 202 + `NVCF-REQID` | polled at `api.nvcf.nvidia.com/v2/nvcf/pexec/status/{id}` |
| reasoning only, empty content | error telling you to raise `max_tokens` or disable thinking |
| `<think>…</think>`, lone `</think>`, `<|return|>` in content | stripped |

## Database (SQLite, `data/intelpod.db`)

| Table | Key | Notes |
|---|---|---|
| `articles` | `article_id` | unique `canonical_url`; indexes on published/discovered time, title, syndication group |
| `events` | `event_id` | stable across runs; centroid terms for linking; first seen / last development |
| `event_articles` | (`event_id`, `article_id`) | which run first linked it |
| `event_scores` | (`run_id`, `event_id`) unique | full `RankedEvent` per run → assessment history |
| `pipeline_runs` | `run_id` | status running/paused/completed/failed, params (incl. `now`) |
| `stage_runs` | (`run_id`, `stage`) unique | status, attempts, output JSON, error |
| `llm_calls` | id | every model call: provider, model, task, tokens, latency, cached, error |
| `llm_cache` | request hash | complete model answers |
| `embedding_cache` | hash(model, text) | float32 vectors, pruned after 14 days |
| `fetch_log` | id | every provider request: status, items, duration, error (quota/health) |
| `feed_state` | feed URL | ETag/Last-Modified, health, consecutive failures |
| `meta` | key | `schema_version` (v2 adds the three tables above; v1 databases upgrade in place) |

Tables for research results, sources/classifications, claims, arguments, impact analyses,
event relationships, forecasts and episodes arrive with the phases that fill them.

## Decisions

| Decision | Why |
|---|---|
| RSS + GDELT first | both free and keyless; RSS gives publisher timestamps and descriptions, GDELT gives breadth across thousands of outlets |
| Lexical clustering by default | runs anywhere in milliseconds, no model download; embeddings optional |
| Server-rendered dashboard | no Node toolchain to install; one process |
| SQLite + SQLAlchemy, `create_all` + version table | zero setup; Alembic would be overkill for a single-user DB at this stage |
| APScheduler / n8n deferred to Phase 7 | the CLI already runs on demand; scheduling belongs with the full pipeline |
| Stage outputs stored as JSON | makes resume trivial and keeps an audit trail of each run |
| One model call per event (top ~12) | small, cacheable, isolated failures; well inside NVIDIA's free rate limit |
| Model scores blended, not substituted | keeps the ranking explainable and stable if the model is wrong or unavailable |
| Ordered model lists | NVIDIA retires free models frequently; a retired default must not break the run |
| Raw httpx instead of an SDK | one small OpenAI-compatible client covers NVIDIA and Ollama; no telemetry, no extra dependency |
