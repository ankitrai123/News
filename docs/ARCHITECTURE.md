# Architecture

## Principles

1. **Zero recurring cost.** Keyless public sources, open-source libraries, SQLite, local models.
2. **Deterministic before expensive.** Normalization, de-duplication, clustering and ranking
   work without any model. Optional models add value on top — embeddings for paraphrase-aware
   clustering and a review of the top events — and are always checked, cached and bounded;
   without them the run produces the same pipeline on rules alone.
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

## Stages

| Stage | Input | Output (persisted in `stage_runs.output`) | Failure behaviour |
|---|---|---|---|
| `ingest` | provider config | per-provider request/item counts, dedup stats, inserted count, warnings | single source failures are logged and skipped; if **all** requests fail and no stored articles are in the window → `StageError` (run fails, resumable); if all fail but older articles exist → continues with a "stale data" warning |
| `cluster` | articles in `[now - lookback, now]` (English) | `EventCandidate` list; events and event–article links written to DB | no articles → `StageError` |
| `rank` | candidates from `cluster` output | `RankedEvent` list in `event_scores` (with full explanation) | retried once |
| `review` | top-ranked events (default 12) | per-event model reviews; outliers unlinked; scores re-ranked and overwritten in `event_scores`; reviewed candidates | **never fails the run**: no key / disabled → `skipped`; provider rejects key, rate limit persists, or every configured model is retired → remaining reviews stop and keyword ranking stands (`failed_all` if none succeeded); one bad answer only affects that event |
| `research` (Phase 2) | selected events (after review) | research packages and every assessed source in `research_*` tables; per-event status, provider notes, page outcomes | **never fails the run** for provider, page or model problems: each event's package records what went wrong; one event's crash marks only that package failed; resumable per event (finished events are skipped). See docs/RESEARCH.md |
| `debate` (Phase 3) | research packages + `config/sources.yaml` | claim map, diversity, viewpoint searches (new sources join the research package), checked model questions/positions; `debate_packages` and `claims` tables | **never fails the run**: one event's failure marks only its package; no model → claim map only (`partial`); resumable per event. See docs/DEBATE.md |
| `impact` (Phase 4) | debate packages + research sources + earlier events | context, impact evidence, checked effect chains, event links, forecast ledger, brief; `impact_analyses`, `event_links`, `forecasts`, `intelligence_briefs` tables | **never fails the run**; no model → evidence tables only (`partial`); resumable per event. See docs/IMPACT.md |
| `report` | ranked events + research + debate + impact | `data/runs/<run_id>/phase1_events.{md,json}` and `research.json` | retried once |

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
  from `taxonomy.yaml` (`semiconductor→chip`, `curbs→restrict`, …), light stemming.
* Countries (default `country_tokens: canonical`): every alias is folded into one token per
  country (Washington/U.S./American → United States, Moscow/Kremlin/Russian → Russia), weighted
  `country_token_weight`, and each pair of countries named together adds a pair token
  (`country_pair_weight`), because "US + Russia" identifies a story far better than either
  country alone. Countries are then *not* also counted as entities, and country names are
  dropped from the proper-noun entities, so one shared country is never counted two or three
  times. `drop` (the behaviour before Oct 2026) removes country words and compares countries
  only as entities.
* TF-IDF vectors over the batch; cosine similarity.
* Entities: capitalised proper-noun phrases that are not country names (plus canonical countries
  in `drop` mode).
* `similarity = cosine + entity_weight × entity_overlap × gate`, where `gate = min(1, cosine/0.2)`.
  Shared entities are a bonus scaled by lexical agreement, so two unrelated stories that both
  mention "United States" do not merge.
* Measured on a labelled set of 627 realistic synthetic headlines (`tests/test_cluster_corpus.py`,
  five days of big/medium stories, look-alike distractors and one-offs), `canonical` vs `drop`:
  B-cubed F1 0.73 vs 0.64, precision 0.83 vs 0.79, a 20+-report story split into 7.9 vs 11.3
  events, and repeated stories in five daily briefings 5 vs 10 slots. Lexical similarity cannot
  match paraphrases that share no words ("Delhi breathes easier as Washington relents on Moscow
  crude"); that is what embeddings are for. Options tried and dropped for lack of effect:
  near-identical-title pre-grouping, a member-reassignment pass, a relaxed final merge, and
  damping or capping country weight.
* Optional embeddings (NVIDIA `nemotron-3-embed-1b` by default, or Ollama), cached per text and
  model; the model that already covers most of the window is reused, and finished batches are
  kept even if a later batch hits a rate limit. Fewer than 4 vectors → embeddings ignored.
  Only the 20 lexically closest clusters per article are compared semantically (speed).
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
An event that looks like another piece of an already selected story (similarity ≥
`duplicate_similarity`, 0.45, *and* shared topic words beyond country names) is skipped, with
"probably the same story as the selected event …" as its reason, so one story split by
clustering does not take several slots. It stays listed under the other events.

**Model review blending.** For reviewed events, each of geopolitical, economic, business, India
and long-term becomes `(1 − w) × keyword + w × model` (`llm.event_review.weight`, default 0.5);
the evidence shows both numbers and the model's one-line reason. A model "soft news" judgement
applies the soft-news penalty. Novelty, corroboration and uncertainty stay rule-based.

These are heuristics over headlines and short descriptions. They are transparent and tunable,
not a judgement of truth.

## Model review safeguards

* The model sees only the retrieved reports (publisher, date, headline, feed summary) with short
  ids (`r1`…): the lead report plus the newest ones (max 12), so evolving stories are judged on the
  latest development. The system prompt tells it to treat report text as data. Unknown ids are
  ignored, and it may remove only a **minority** of reports: a verdict removing half or more is
  ignored and recorded (defence against instructions injected into a headline).
* The answer must validate against a pydantic schema with five explicit score fields (requested
  via `response_format: json_schema`); invalid JSON gets one repair round-trip.
* **Grounding check:** a model title or summary is rejected if it contains any number or
  capitalised name that appears, as a whole word, in none of the reports that stay in the event
  (outliers excluded); the publisher headline is kept instead. Rejections are recorded and shown.
* Outliers are unlinked from the event, its countries/topic/entities are recomputed, and the
  removed reports are regrouped into event(s) of their own so they are still ranked.
* Re-ranking with blended scores can promote events that were not reviewed into the briefing;
  those get follow-up reviews (`llm.event_review.max_extra_reviews`, default 6) and any still
  unreviewed are flagged `not_model_reviewed`.
* Every call is logged (`llm_calls`: model, tokens, latency, error). Only answers that are
  complete (`finish_reason: stop`) **and validated** are cached (`llm_cache`).

## LLM client behaviour (NVIDIA specifics from NVIDIA's client source and forum staff)

| Situation | Handling |
|---|---|
| No key / disabled | stage `skipped`, reason recorded |
| 401/402/403 | provider unusable for this run; 403 message explains the "Public API Endpoints" entitlement |
| 404 "not found for account" / 410 Gone | that model is skipped for the run; next model in `llm.tasks.<task>.models`; if all answer 404 the error points at the key's entitlement |
| 5xx after retries, timeout | that model is skipped for the run (timeouts are not retried); next model |
| 422 "body -> field … extra_forbidden" | field dropped for that model, retried once |
| 400/422 on structured output | retried with prompt-only JSON for that model |
| 429 (no Retry-After) | backoff 5 → 10 → 20 s, then stop calling for this run (the limit is per account) |
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
| Lexical clustering as the base | works anywhere with no model; embeddings (NVIDIA by default when a key is set, or Ollama) only boost it |
| Server-rendered dashboard | no Node toolchain to install; one process |
| SQLite + SQLAlchemy, `create_all` + version table | zero setup; Alembic would be overkill for a single-user DB at this stage |
| APScheduler / n8n deferred to Phase 7 | the CLI already runs on demand; scheduling belongs with the full pipeline |
| Stage outputs stored as JSON | makes resume trivial and keeps an audit trail of each run |
| One model call per event (top ~12) | small, cacheable, isolated failures; well inside NVIDIA's free rate limit |
| Model scores blended, not substituted | keeps the ranking explainable and stable if the model is wrong or unavailable |
| Ordered model lists | NVIDIA retires free models frequently; a retired default must not break the run |
| Dashboard refuses cross-site writes and unknown Host headers | starting a run spends NewsData credits and NVIDIA requests; blocks CSRF and DNS rebinding |
| API keys read via `read_secret` (stripped; malformed refused) and all error text redacted | a key with a trailing newline used to surface inside an HTTP error message |
| Raw httpx instead of an SDK | one small OpenAI-compatible client covers NVIDIA and Ollama; no telemetry, no extra dependency |
