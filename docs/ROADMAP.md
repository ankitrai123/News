# Roadmap, acceptance status and known limitations

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Ingestion (RSS, GDELT, NewsData.io), normalization, de-duplication, event clustering (+ embeddings), ranking, model review (NVIDIA/Ollama), SQLite, CLI/report/dashboard | **Done** — tested offline with mocked APIs; awaiting first live run with real keys |
| 2 | Research: per-event query generation (10 query types), archive/feeds/GDELT/Guardian adapters, 10–21 day window, page fetching within robots/paywall/opt-out rules, date validation, 10–15 results per event, source-quality assessment, persistence and caches, dashboard/API/CLI (docs/RESEARCH.md) | **Done** — tested offline with mocked services; awaiting a live run |
| 3 | (see below) | next |
| 3 | Source registry with evidence-backed editorial metadata, viewpoint discovery, claim-level evidence map, agreement/disagreement, uncertainty | planned |
| 4 | Historical context, event relationships, causal business/economic chains, India impact | planned |
| 5 | Narrative plan, two-host script, citation-linked transcript, fact verification | planned |
| 6 | Local TTS (Piper/Kokoro evaluated), two voices, FFmpeg assembly, audio checks | planned |
| 7 | Scheduling (APScheduler; optional n8n export), notifications, retry policies, quota tracking, end-to-end tests | planned |

## Final acceptance criteria — where Phase 1 stands

| Criterion | Status |
|---|---|
| Ingests current news from at least one functioning source | Implemented for 21 RSS feeds + GDELT + NewsData.io. **Not yet observed live**: the build sandbox's network policy blocked every news host (`ProxyError 403`). Run `intelpod feeds check && intelpod run` on your machine. |
| Normalizes and de-duplicates articles | Done (tested) |
| Groups articles into meaningful events | Done (tested on synthetic data; tune on real data) |
| Ranks events transparently | Done (8 explained components, penalties, written rationale) |
| Stores previous research / resumes interrupted work | Done for Phase 1 data |
| Operates without mandatory paid services | Done |
| Documents every external dependency | Done (docs/PROVIDERS.md) |
| Researches recent online discussion | Phase 2 done (offline tests; live run pending): archive, research feeds, GDELT date-window search, optional Guardian; packages with explicit limitations |
| Cites actual supporting sources | Phase 2 stores only retrieved, date-checked sources with URLs; model notes are rejected unless grounded in the source's own extracts |
| Viewpoints, claims, business analysis, script, verification, audio, sample episode | Phases 3–6 |

## Known limitations (Phase 1)

* **No live verification yet.** All feed URLs are marked `verified: false`; some may have moved.
  The pipeline handles that (logs, skips, flags) but cannot fix it.
* **NewsData.io and NVIDIA integrations are tested only against mocked APIs** built from their
  documented formats; the build sandbox could not reach either service and had no keys. Run
  `intelpod newsdata test`, `intelpod llm test` and `pytest -m live` once your keys are set.
* **NVIDIA model ids are third-party-reported** (Oct 2026) and retire often; the failover list
  softens this but you may need to update it.
* **NVIDIA trial terms:** "internal testing and evaluation, not production"; personal use is not a
  named category. Do not publish model-written text from the free tier; use Ollama or a paid plan
  for ongoing or shared use.
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

## Known limitations (Phase 2)

* **No live run yet**: GDELT, the research feeds, the Guardian API and publisher pages could not be
  reached from the build sandbox. Research feed URLs are `verified: false`; run
  `intelpod feeds check --research`.
* **No general web search**: no free, permitted web-search API exists in 2026 (Brave needs a card
  and forbids storing results; Google CSE closed; Bing retired), so discovery is limited to the
  archive, the configured feeds, GDELT and the Guardian.
* **Source quality is partial**: track record and corrections policy are "unknown" until Phase 3's
  evidence-backed source registry.
* **Relevance is lexical** (calibrated: 89% recall / 0.67 precision on the labelled corpus);
  embeddings are not yet used in research.

## Exact next step

Phase 3, step 1: a source registry (`config/sources.yaml` + table) with documented,
human-reviewable editorial metadata (orientation per issue dimension, evidence links,
confidence, last reviewed; "unknown" allowed), used by research to fill `editorial_metadata`
and the track-record / corrections components; then viewpoint discovery per interpretive
question with claim-level evidence maps.

Before that: run Phases 1–2 against live sources (`intelpod feeds check`, `intelpod feeds check
--research`, `intelpod run`), confirm which research feeds and GDELT queries work, and review a
few research packages for relevance.
