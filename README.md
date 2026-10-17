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

* **Sources:** 22 public RSS feeds (international, Indian and official/central-bank
  sources) plus GDELT's free DOC 2.0 API with 9 topic queries. All configurable.
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

`intelpod init` never overwrites your config. New sections (`ingestion.providers.newsdata`,
`clustering.embeddings`, `llm`) have working defaults when absent, but a Phase-1 config that sets
`clustering.embeddings.enabled: false` keeps embeddings off — compare with
`config/config.example.yaml` and copy what you want.

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
pytest                 # 142 offline tests (HTTP mocked with respx, synthetic fixtures; real keys ignored)
pytest -m live -v      # 5 real-network tests: GDELT, a feed, NewsData (1 credit), NVIDIA chat + embeddings
```

Offline tests use **synthetic** fixtures (fictional publishers on `.test` domains), clearly
separated from the real-network tests in `tests/test_live.py`.

## Configuration

| File | Purpose |
|---|---|
| `config/config.yaml` | everything: lookback window, providers, GDELT queries, clustering thresholds, ranking weights, selection limits |
| `config/feeds.yaml` | RSS feed registry (publisher, country, region, source type, enabled) |
| `config/taxonomy.yaml` | topic keywords, India terms, significance terms, soft-news terms, synonyms |
| `.env` | `NEWSDATA_API_KEY`, `NVIDIA_API_KEY` (both optional) and `INTELPOD_*` overrides (see `.env.example`) |

## Documentation

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — design, data flow, schemas, database, decisions
* [docs/PROVIDERS.md](docs/PROVIDERS.md) — every external dependency, free-tier limits, licences,
  and the NVIDIA trial terms ("internal testing and evaluation, not production"; don't publish
  model-written text from it — use Ollama for ongoing or shared use)
* [docs/API.md](docs/API.md) — local HTTP API
* [docs/OLLAMA.md](docs/OLLAMA.md) — installing Ollama and choosing models for your hardware
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
* [docs/PRIVACY.md](docs/PRIVACY.md) — what is stored, retention, copyright
* [docs/ROADMAP.md](docs/ROADMAP.md) — phases, known limitations, next step
* [docs/examples/phase1_report_SYNTHETIC.md](docs/examples/phase1_report_SYNTHETIC.md) — report format (synthetic data, not real news)

## Licence

MIT for this code. Third-party software and data sources keep their own licences and terms
(see docs/PROVIDERS.md).
