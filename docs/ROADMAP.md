# Roadmap, acceptance status and known limitations

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Ingestion (RSS, GDELT, NewsData.io), normalization, de-duplication, event clustering (+ embeddings), ranking, model review (NVIDIA/Ollama), SQLite, CLI/report/dashboard | **Done** — tested offline with mocked APIs; awaiting first live run with real keys |
| 2 | Research: per-event query generation (10 query types), GDELT/RSS/official-source adapters, 10–21 day window, date validation, 10–15 results per event, source-quality assessment, cache. Uses the same `LLMClient` (new task entries in `llm.tasks`) | next |
| 3 | Source registry with evidence-backed editorial metadata, viewpoint discovery, claim-level evidence map, agreement/disagreement, uncertainty | planned |
| 4 | Historical context, event relationships, causal business/economic chains, India impact | planned |
| 5 | Narrative plan, two-host script, citation-linked transcript, fact verification | planned |
| 6 | Local TTS (Piper/Kokoro evaluated), two voices, FFmpeg assembly, audio checks | planned |
| 7 | Scheduling (APScheduler; optional n8n export), notifications, retry policies, quota tracking, end-to-end tests | planned |

## Final acceptance criteria — where Phase 1 stands

| Criterion | Status |
|---|---|
| Ingests current news from at least one functioning source | Implemented for 22 RSS feeds + GDELT. **Not yet observed live**: the build sandbox's network policy blocked every news host (`ProxyError 403`). Run `intelpod feeds check && intelpod run` on your machine. |
| Normalizes and de-duplicates articles | Done (tested) |
| Groups articles into meaningful events | Done (tested on synthetic data; tune on real data) |
| Ranks events transparently | Done (8 explained components, penalties, written rationale) |
| Stores previous research / resumes interrupted work | Done for Phase 1 data |
| Operates without mandatory paid services | Done |
| Documents every external dependency | Done (docs/PROVIDERS.md) |
| Research, viewpoints, claims, business analysis, script, verification, audio, sample episode | Phases 2–6 |

## Known limitations (Phase 1)

* **No live verification yet.** All feed URLs are marked `verified: false`; some may have moved.
  The pipeline handles that (logs, skips, flags) but cannot fix it.
* **NewsData.io and NVIDIA integrations are tested only against mocked APIs** built from their
  documented formats; the build sandbox could not reach either service and had no keys. Run
  `intelpod newsdata test`, `intelpod llm test` and `pytest -m live` once your keys are set.
* **NVIDIA model ids are third-party-reported** (Oct 2026) and retire often; the failover list
  softens this but you may need to update it.
* **NVIDIA trial terms:** personal/evaluation use only; do not publish model-written text from the
  free tier (use Ollama or a paid plan for anything public).
* **NewsData free plan is ~12 h delayed** and 200 credits/day.
* **Headline-level analysis.** Clustering and ranking use titles plus feed summaries (GDELT gives
  titles only). Keyword heuristics miss nuance and can misjudge significance; weights and
  thresholds need tuning on real data.
* **English only.** Non-English articles are stored but not clustered.
* **Country/entity extraction is a gazetteer + capitalisation heuristic,** not real NER
  (e.g. "Georgia" and "Jordan" are deliberately not mapped).
* **GDELT dates are "first seen" times,** not publication times (flagged per event).
* **Syndication detection is title-based;** rewritten wire copy counts as independent.
* **Greedy clustering is order-dependent** at the margins; the merge pass reduces but does not
  eliminate this.
* **Settings page is read-only;** edit `config/config.yaml`.
* **No scheduler yet** (Phase 7); run `intelpod run` manually or from cron/Task Scheduler.

## Exact next step

Phase 2, step 1: add a `research` stage after `rank` that, for each selected event, generates
the ten query types (latest facts, recent history, political and economic interpretations,
business consequences, India implications, expert analysis, primary sources, criticism,
open questions) — deterministic templates first, with an optional Ollama rewrite — and runs
them against GDELT with a configurable 10–21 day `startdatetime/enddatetime` window plus
official-source feeds, persisting de-duplicated, date-validated results in a new
`research_results` table keyed by `event_id`.

Before that: run Phase 1 against live sources, record which feeds work, and tune
`similarity_threshold` and ranking weights on a few days of real output.
