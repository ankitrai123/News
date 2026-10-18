# IntelPod — geopolitical & business intelligence podcast agent

A local, zero-cost application that turns global news into a researched,
multi-perspective two-host podcast. It runs on your own computer with free and
open-source software, keyless public data sources, and two optional free tiers:
**NewsData.io** for extra news coverage and **NVIDIA's API Catalog** for the language model
and embeddings (or local models through Ollama). No paid service is required; without any
keys IntelPod still runs end to end on RSS + GDELT with keyword-based analysis.

> **Status: Phase 1 of 7 is complete.** IntelPod currently ingests news, normalizes and
> de-duplicates articles, groups them into events, ranks the events transparently, stores
> everything in SQLite, and shows the result in a CLI, a Markdown/JSON report and a local
> dashboard. Research, viewpoint analysis, business impact, script writing and audio come
> in Phases 2–7 (see [docs/ROADMAP.md](docs/ROADMAP.md)). No module in this repository is
> an empty placeholder.

## What Phase 1 does

```
RSS/Atom feeds ─┐
GDELT DOC 2.0 ──┼─► normalize ─► de-duplicate ─► cluster into events ─► rank ─► model review ─► report + dashboard
NewsData.io ────┘   (canonical URL, (URL, same-publisher  (TF-IDF + entities  (8 explained   (NVIDIA LLM: title,
  (optional key)     UTC dates,      title, syndication    + NVIDIA/Ollama     criteria,      summary, outliers,
                     short extracts) groups)               embeddings)         penalties)     significance; checked)
```

* **Sources:** 21 public RSS feeds (international, Indian and official/central-bank
  sources; 2 more listed but disabled) plus GDELT's free DOC 2.0 API with 5 topic queries. All configurable.
* **De-duplication:** canonical URLs (tracking parameters, `www`, `amp`, fragments removed),
  same-headline-same-publisher detection, and *syndication groups* so wire copies carried by
  several outlets count as one source.
* **Event clustering:** articles, not events, are the input; "US announces chip export
  controls", "Washington tightens restrictions on chip exports" and "New American measures
  target China's chip industry" become one event. Ongoing stories keep a stable event ID
  across days.
* **Ranking:** geopolitical, economic, business, India relevance, novelty, long-term
  consequences, independent corroboration and uncertainty — each scored 0–1 with the evidence
  behind it, weighted by your config. Article volume never counts directly. Each selected
  event gets a written "why selected".
* **Model review (optional, NVIDIA free tier or local Ollama):** the top ~12 events are each
  reviewed by a language model that sees only the retrieved reports. It flags off-topic reports
  (removed), writes a neutral title and short summary, and scores five significance dimensions.
  IntelPod rejects any title/summary that names people, places or numbers absent from the
  reports, and **blends** the model's scores with the keyword scores (both shown). If NVIDIA
  retires a model, the next one in the configured list is used.
* **Selection:** 5–8 events by default; fewer on quiet days, with a cap per topic.
* **Resumable runs:** every stage persists its output; `intelpod run --resume` continues an
  interrupted run without re-fetching.

## What Phase 2 adds: research packages

For each selected event, the `research` stage searches the previous 14 days (configurable
10–21) for ~10–15 sources — reporting, analysis, opinion, official statements and think-tank
research — through IntelPod's own article archive, official/think-tank feeds
(`config/research_feeds.yaml`), GDELT's date-window search and, with a free key, the Guardian.
It reads result pages only where robots.txt, paywalls and publisher opt-outs allow, keeps
short verbatim extracts (never full articles), validates publication dates against the event
date, scores relevance and source quality with the evidence shown, picks a diverse set, and
states every limitation ("no primary source found", "3 pages paywalled"). Optional NVIDIA/Ollama
help proposes search keywords and writes notes that are rejected if they name anything absent
from the source's own extracts. Details: [docs/RESEARCH.md](docs/RESEARCH.md).

```bash
intelpod run                       # Phase 1 + Phase 2 + Phase 3 + report
intelpod run --stop-after review   # Phase 1 only
intelpod research                  # Phase 2 on the latest run (or --run-id)
```

## What Phase 3 adds: viewpoints and evidence

The `debate` stage maps each event's claims — verbatim statements from the retrieved sources,
typed as fact, attributed statement, interpretation, forecast or value judgement — groups them
across sources and labels each **confirmed, reported, plausible, contested or speculative**. It
shows who is represented (countries, advanced/emerging economies, source types, quoted voices),
runs targeted searches for missing perspectives, and (with a model) identifies interpretive
questions on which credible sources genuinely disagree. IntelPod checks every model-written
position against the sources it cites, computes evidence strength itself, refuses to present a
one-sided question as a debate, and says so when there is no meaningful disagreement. Editorial
orientation comes only from documented, referenced entries in `config/sources.yaml` (all
`unknown` by default). Details: [docs/DEBATE.md](docs/DEBATE.md).

```bash
intelpod debate                    # Phase 3 on the latest researched run (or --run-id)
intelpod sources --gaps            # feed publishers missing from the source registry
```

## Quick start

Requirements: Python 3.11+ (developed on 3.13), internet access. Works on Linux, macOS and
Windows.

```bash
git clone <this repo> intelpod && cd intelpod
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .

intelpod init                      # creates config/config.yaml and data/intelpod.db
# edit config/config.yaml -> app.user_agent: put your contact e-mail (polite to publishers)
cp .env.example .env               # optional: add NEWSDATA_API_KEY and NVIDIA_API_KEY
intelpod doctor                    # hardware, keys, model list, source reachability
intelpod feeds check               # fetch every feed once; disable any that fail
intelpod newsdata test             # optional: 1 request with your NewsData key (1 credit)
intelpod llm models                # optional: models your NVIDIA key can use
intelpod llm test                  # optional: one small call to check key + model
intelpod run                       # full Phase 1 pipeline (~1–3 minutes)
intelpod serve                     # dashboard at http://127.0.0.1:8000
```

The report is written to `data/runs/<run_id>/phase1_events.md` (and `.json`).

### Upgrading an existing `config/config.yaml`

`intelpod init` never overwrites your config. New settings have working defaults when absent,
but values your file *does* set win. Compare with `config/config.example.yaml`, in particular:

* `clustering.embeddings.enabled: false` (older configs) keeps embeddings off. Embeddings are
  the biggest help against one story being split into many events; turn them on and set
  `NVIDIA_API_KEY` (or use Ollama). The briefing page says whether they were used.
* `ingestion.providers.gdelt`: older configs list 9 queries 6 seconds apart,
  which GDELT throttles (HTTP 429). The example now uses 5 broad queries, 10 s spacing and a
  cool-down; copy that section.
* New, defaulted when absent: `clustering.country_tokens` / `country_token_weight` /
  `country_pair_weight`, `ranking.duplicate_similarity`, `ingestion.feed_pause_after_failures`,
  `ingestion.feed_retry_paused_after_hours`, `ingestion.providers.rss.max_workers`, GDELT
  `cooldown_seconds` / `max_cooldown_seconds` / `stop_after_refusals`, taxonomy `label_aliases`,
  per-feed `timezone`, and the whole `research` section (Phase 2).
* The first run after this upgrade may link a few continuing stories to earlier events less
  reliably (stored event profiles were built with the older country handling); it settles after one run.

### Running each stage on its own

```bash
intelpod run --stop-after ingest               # fetch + normalize + dedup + store, then pause
intelpod run --resume                          # continue the latest run (cluster, rank, report)
intelpod run --resume --run-id run_2026...     # continue a specific run
intelpod run --no-gdelt                        # skip a source (also --no-rss, --no-newsdata)
intelpod run --no-llm                          # no model review and no NVIDIA embeddings: nothing sent to NVIDIA
intelpod run --lookback-hours 24
intelpod events --all                          # every scored event, selected or not
intelpod runs                                  # run history, stage status, request counts
```

### Tests

```bash
pip install -e ".[dev]"
pytest                 # 221 offline tests (HTTP mocked with respx, synthetic fixtures; real keys ignored)
pytest -m live -v      # 7 real-network tests: GDELT (news + research window), a feed, a public page, NewsData (1 credit), NVIDIA chat + embeddings
```

Offline tests use **synthetic** fixtures (fictional publishers on `.test` domains), clearly
separated from the real-network tests in `tests/test_live.py`. Clustering quality is measured on
`tests/fixtures/clustering_corpus.json` (627 model-written headlines with known story labels);
`python -m tests.test_cluster_corpus [key=value ...]` prints the scores for a setting.

## Configuration

| File | Purpose |
|---|---|
| `config/config.yaml` | everything: lookback window, providers, GDELT queries, clustering thresholds, ranking weights, selection limits |
| `config/feeds.yaml` | RSS feed registry (publisher, country, region, source type, enabled) |
| `config/sources.yaml` | source registry: documented publisher facts (country, ownership, type) with references; orientation `unknown` unless referenced (Phase 3) |
| `config/research_feeds.yaml` | research feeds: official bodies, international organisations, think tanks (Phase 2) |
| `config/taxonomy.yaml` | topic keywords, India terms, significance terms, soft-news terms, synonyms |
| `.env` | `NEWSDATA_API_KEY`, `NVIDIA_API_KEY`, `GUARDIAN_API_KEY` (all optional) and `INTELPOD_*` overrides (see `.env.example`) |

## Documentation

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design, data flow, schemas, database, decisions
* [docs/PROVIDERS.md](docs/PROVIDERS.md) — every external dependency, free-tier limits, licences,
  and the NVIDIA trial terms ("internal testing and evaluation, not production"; don't publish
  model-written text from it — use Ollama for ongoing or shared use)
* [docs/DEBATE.md](docs/DEBATE.md) — Phase 3: claim map, confidence labels, diversity, viewpoint searches, model checks
* [docs/RESEARCH.md](docs/RESEARCH.md) — Phase 2: research questions, providers, page rules, dates, quality, selection
* [docs/API.md](docs/API.md) — local HTTP API
* [docs/OLLAMA.md](docs/OLLAMA.md) — installing Ollama and choosing models for your hardware
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
* [docs/PRIVACY.md](docs/PRIVACY.md) — what is stored, retention, copyright
* [docs/ROADMAP.md](docs/ROADMAP.md) — phases, known limitations, next step
* [docs/examples/phase1_report_SYNTHETIC.md](docs/examples/phase1_report_SYNTHETIC.md) — report format (synthetic data, not real news)

## Licence

MIT for this code. Third-party software and data sources keep their own licences and terms
(see docs/PROVIDERS.md).
