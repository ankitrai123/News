# External dependencies, free tiers and licences

IntelPod never needs a paid service. This page lists every external dependency, its cost
category, known limits and caveats. **Free services can change their terms; nothing here is
a guarantee of permanent free availability.** Facts marked *unverified* could not be checked
from the build environment (its network policy blocked news hosts) — confirm them with
`intelpod doctor` and `intelpod feeds check` on your machine.

## Cost categories

| Category | Used in Phase 1 |
|---|---|
| Free & open-source software (runs locally) | Python libraries below, SQLite, FFmpeg (detected, used from Phase 6), Ollama (optional local alternative to NVIDIA) |
| Free public service, no key | GDELT DOC 2.0 API, publishers' RSS/Atom feeds |
| Free tier, registration + API key, quota/rate-limited | **NewsData.io** (news), **NVIDIA API Catalog** (language model + embeddings) |
| Paid | none, and none required later |

Both keyed services are **optional**: without `NEWSDATA_API_KEY` the NewsData provider is
skipped; without `NVIDIA_API_KEY` clustering uses lexical similarity only and the model review
stage is skipped, so ranking is keyword-based. Every run records which of these happened.

## News sources

### GDELT DOC 2.0 API — free, no registration

* Endpoint: `https://api.gdeltproject.org/api/v2/doc/doc` with `mode=ArtList&format=json`.
* Returns article **metadata only**: URL, title, `seendate`, domain, language, source country.
  No description or body text.
* `seendate` is when GDELT first saw the URL, not the publisher's timestamp; IntelPod stores it
  with `date_source = "gdelt_seendate"` and flags such events `some_dates_approximate`.
* Search window: GDELT's blog states that non-timeline modes cover the most recent ~3 months.
* `maxrecords` up to 250 per request (IntelPod default 75).
* Rate limiting: GDELT asks clients to throttle and replies with a plain-text notice instead of
  JSON when requests come too fast; IntelPod spaces requests 6 s apart (configurable) and
  treats that notice as an error for that query only. The exact current threshold is
  *unverified*; GDELT has historically asked for about one request every 5 seconds.
* Coverage is broad but not complete; it does not index every outlet, paywalled text or social
  media.
* Terms: GDELT describes its data as open for unrestricted use with citation; *verify on
  gdeltproject.org* before any non-personal use.

### RSS/Atom feeds — free, no registration

`config/feeds.yaml` lists 22 enabled feeds (+1 disabled): BBC (world, business), The Guardian
(world, business), Al Jazeera, NPR, CNBC, Fox News, France 24, DW, SCMP, Nikkei Asia, The Hindu
(international, business), The Indian Express, The Economic Times, Mint, The Times of India,
US Federal Reserve, ECB, Reserve Bank of India and UN News.

* All URLs are the publishers' publicly listed feeds as known at development time and are marked
  `verified: false` until `intelpod feeds check` confirms them. Publishers move feeds without
  notice; disable or update any that fail.
* Feeds are designed for syndication, but each publisher's terms apply. Many (e.g. the BBC) permit
  personal, non-commercial use of their feeds. IntelPod keeps titles, links, timestamps and the
  feed's own short summary (≤ 400 chars) only.
* IntelPod honours `robots.txt`, sends conditional GETs (ETag / Last-Modified), throttles to one
  request per second per host and identifies itself with the configurable `user_agent`.
* Google News RSS is deliberately **not** included: its terms for automated use are unclear.

### NewsData.io — free tier, registration + API key (`NEWSDATA_API_KEY`)

Facts below come from NewsData's own OpenAPI spec (read through a byte-for-byte mirror,
because newsdata.io was unreachable from the build environment), its official Python/Go SDK
source, and real captured responses; each was independently re-checked. Re-verify on
newsdata.io before relying on them.

* Endpoint used: `GET https://newsdata.io/api/1/latest` (news from the past 48 hours).
  Key sent in the `X-ACCESS-KEY` header, so it never appears in URLs or logs.
* **Free plan: 200 credits/day; 30 credits per 15 minutes; 1 credit per request returning up
  to 10 articles** (`size` ≤ 10). `q` ≤ 100 characters (AND/OR/NOT, parentheses, quotes).
* **Free-plan articles are delayed about 12 hours**, so they are 12–48 h old. NewsData adds
  breadth (thousands of sources, good Indian coverage), not freshness; RSS stays the freshest source.
* Not on free: `timeframe`, archive/date ranges, counts, sentiment/AI tags, full article content.
  Those fields still appear in responses holding notices like `"ONLY AVAILABLE IN PAID PLANS"`;
  IntelPod discards any such value.
* HTTP 429 has three meanings: a short burst limit (`TooManyRequests`, IntelPod retries twice
  with backoff), the 15-minute window (`RateLimitExceeded`) and an exhausted daily quota
  (`ApiLimitExceeded`/`ApiKeyLimitExceeded`); the last two stop NewsData for the rest of the run.
  The `X-API-Limit-Remaining` header (credits left) is recorded in each run's ingest summary.
* IntelPod's own budget: at most `max_credits_per_run` (default 20) and `daily_credit_budget`
  (default 180) requests per UTC day, counted from its request log. The exact time NewsData
  resets the daily quota is not documented.
* Free keys reportedly become inactive after ~30 days without use (then: 401; regenerate it).
* **Terms:** NewsData's pages contradict each other on whether the free plan allows
  commercial use; treat it as personal/evaluation use. Its FAQ permits displaying title,
  short description, publisher, author and date with credit to the publisher — which is all
  IntelPod stores or shows.
* Get a key: sign up at <https://newsdata.io> (no card); the key is on the dashboard.

### NVIDIA API Catalog — free trial, registration + API key (`NVIDIA_API_KEY`)

Used for (1) the **event review** language model and (2) **embeddings** for clustering. Facts
come from NVIDIA's own client library source (`langchain-nvidia-ai-endpoints` 1.5.0), the NVIDIA
API Trial Terms of Service (v. 19 Sep 2025, read through a verbatim mirror) and NVIDIA developer
forum staff replies; NVIDIA's hosts were unreachable from the build environment. Re-verify on
build.nvidia.com.

* OpenAI-compatible endpoints at `https://integrate.api.nvidia.com/v1`: `/chat/completions`,
  `/embeddings`, `/models`. Key format `nvapi-…`, sent as `Authorization: Bearer`.
* **Cost: $0** with free NVIDIA Developer Program membership. The old credit system was replaced
  by **per-account rate limits** ("up to" ~40 requests/minute, varying by model and load; users
  report 429s from ~12–24/min; no `Retry-After` header; no published daily quota). IntelPod paces
  requests at 20/minute (shared by chat and embeddings), backs off 5→10→20 s on 429, caches every
  answer and embedding, and makes about 12 chat calls plus a few embedding calls per run.
* **Terms (important):** the free tier is a *trial* "for internal testing and evaluation purposes,
  not in production" (§1.2/§1.4), and you may not "make available to others any portion of … Generated
  Content" (§4.2). IntelPod as a private, personal briefing fits this; **publishing its model-written
  text (e.g. a public podcast) would need a paid NVIDIA subscription or a different model provider**
  (local Ollama has no such restriction beyond each model's licence). NVIDIA may log requests and use
  prompts and outputs to improve its services (§3.3): IntelPod sends only public headlines and short
  feed summaries, never personal data. The service and individual models can change or end at any time.
* **Models are retired often.** Between August and October 2026 most well-known free ids
  (`meta/llama-3.3-70b-instruct`, `meta/llama-3.1-8b-instruct`, `nvidia/nemotron-3-nano-30b-a3b`,
  `deepseek-ai/deepseek-v4-flash`, `openai/gpt-oss-120b`, …) were reported returning **410 Gone**.
  IntelPod therefore keeps an ordered model list per task and skips any model answering 404/410.
  The defaults (`nvidia/nemotron-3.5-lightning-30b-a3b`, `openai/gpt-oss-20b`, `google/gemma-4-31b-it`,
  `nvidia/nemotron-3-ultra-550b-a55b`; embeddings `nvidia/nemotron-3-embed-1b` then
  `nvidia/nv-embedqa-e5-v5`) were reported live in Oct 2026 **by third parties** — confirm with
  `intelpod llm models` and `intelpod llm test --model <id>`.
* Reasoning ("thinking") is switched off per model where supported
  (`chat_template_kwargs.enable_thinking=false`; `reasoning_effort=low` for gpt-oss) because
  reasoning tokens consume the output budget.
* Structured output uses `response_format: {type: json_schema}`; IntelPod still validates every
  answer, retries once on invalid JSON, and falls back to prompt-only JSON if a model rejects the field.
* A **403 "Authorization failed" while `/v1/models` works** usually means the key lacks the
  "Public API Endpoints" entitlement. Often it cannot be selected when creating the key; NVIDIA support
  (help@build.nvidia.com) has to enable it.
* Model licences apply on top (e.g. NVIDIA Open Model License, Gemma terms, Apache-2.0 for gpt-oss).
* Get a key: sign in at <https://build.nvidia.com>, then *Get API Key* on a model page or
  <https://build.nvidia.com/settings/api-keys>.

### Discovered via public-apis

The [public-apis News list](https://github.com/public-apis/public-apis#news) includes other
services with free tiers (e.g. GNews, Currents, New York Times). NewsData.io from that list is
integrated (above). Others can be added as one class implementing `intelpod/providers/base.py`;
keys go in `.env` and anything named `*_KEY`/`*_TOKEN` is redacted from logs.

## Local software

| Software | Version (dev) | Licence | Role |
|---|---|---|---|
| Python | 3.13 (3.11+ supported) | PSF | runtime |
| FastAPI | 0.143 | MIT | API + dashboard |
| Uvicorn | 0.54 | BSD-3-Clause | web server |
| SQLAlchemy | 2.1 | MIT | database layer |
| SQLite | bundled with Python | public domain | storage |
| httpx | 0.28 | BSD-3-Clause | HTTP client |
| feedparser | 6.0 | BSD-2-Clause | RSS/Atom parsing |
| pydantic | 2.14 | MIT | schemas/validation |
| PyYAML | 6.0 | MIT | config |
| Jinja2 | 3.1 | BSD-3-Clause | templates |
| RapidFuzz | 3.14 | MIT | fuzzy title matching |
| python-multipart | 0.0.32 | Apache-2.0 | form handling |
| pytest / respx | 9.1 / 0.23 | MIT / BSD-3-Clause | tests only |
| Ollama (optional) | — | MIT | local alternative to NVIDIA for embeddings and the model review |
| FFmpeg (detected) | — | LGPL/GPL depending on build | audio assembly from Phase 6 |

Licences above were read from installed package metadata. Model weights pulled through Ollama
have **their own** licences (e.g. Apache-2.0, Llama Community License, Qwen licence, Gemma
terms); check each model's page before use.

## Optional paid upgrades (never required)

Paid NewsData plans remove the 12-hour delay and add full content; a paid NVIDIA subscription
allows production use and higher limits. IntelPod works without either, and integrates neither.
