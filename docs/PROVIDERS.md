# External dependencies, free tiers and licences

IntelPod never needs a paid service. This page lists every external dependency, its cost
category, known limits and caveats. **Free services can change their terms; nothing here is
a guarantee of permanent free availability.** Facts marked *unverified* could not be checked
from the build environment (its network policy blocked news hosts) — confirm them with
`intelpod doctor` and `intelpod feeds check` on your machine.

## Cost categories

| Category | Used in Phase 1 |
|---|---|
| Free & open-source software (runs locally) | Python libraries below, SQLite, FFmpeg (detected, used from Phase 6), Ollama (optional) |
| Free public service, no key | GDELT DOC 2.0 API, publishers' RSS/Atom feeds |
| Free API tier with quota / key required | none (interface ready for future optional providers) |
| Paid | none, and none required later |

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

### Discovered via public-apis

The [public-apis News list](https://github.com/public-apis/public-apis#news) includes several
services with free tiers (e.g. NewsData.io, GNews, Currents, New York Times). They need
registration and API keys and have daily quotas and, often, non-commercial or attribution
conditions. None is required. The provider interface (`intelpod/providers/base.py`) makes adding
one a single class; keys would go in `.env` and are redacted from logs.

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
| Ollama (optional) | — | MIT | local embeddings now, local LLMs from Phase 2 |
| FFmpeg (detected) | — | LGPL/GPL depending on build | audio assembly from Phase 6 |

Licences above were read from installed package metadata. Model weights pulled through Ollama
have **their own** licences (e.g. Apache-2.0, Llama Community License, Qwen licence, Gemma
terms); check each model's page before use.

## Optional paid upgrades (never required)

Hosted LLM APIs, commercial news APIs or cloud TTS could improve quality or coverage, but IntelPod
is designed so that none of them is needed, and none is integrated.
