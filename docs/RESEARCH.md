# Phase 2 — Internet research

For each event selected in Phase 1, IntelPod builds a **research package**: about 10–15
sources from the previous `research.window_days` (default 14, spec 10–21), each with its
publication date, kind of source, relevance, a transparent quality assessment and short
verbatim extracts — plus an honest list of what could not be found.

```
selected event ─▶ queries (10 research questions) ─▶ search providers ─▶ merge & de-duplicate
      ─▶ fetch the most promising pages (robots.txt, paywalls and opt-outs respected)
      ─▶ dates · kind of source · relevance · quality · short extracts
      ─▶ optional model notes (checked) ─▶ select 10–15 diverse sources ─▶ package + limitations
```

## Running it

```bash
intelpod run                          # Phase 1 + Phase 2 + report
intelpod run --stop-after review      # Phase 1 only; then either:
intelpod run --resume                 #   continue (research + report), or
intelpod research                     #   research the latest run's selected events
intelpod research --run-id RUN --refresh   # research a run again (replaces its packages)
intelpod run --no-research            # skip Phase 2
intelpod feeds check --research       # check the research feeds
```

The dashboard shows a "Research" line under each briefing event and a full research section on
the event page (sources, extracts, quality breakdown, limitations, the searches that were run,
and the sources considered but not used, each with its reason).

## The ten research questions

| Query type | What it looks for |
|---|---|
| latest facts | newest reporting on the event (last `latest_days`, newest first) |
| recent history | what happened in the preceding days and weeks |
| political interpretations | how politicians, parties and governments read it |
| economic interpretations | economists, markets, macro effects |
| business and industry | companies, sectors, supply chains |
| India implications | Indian outlets and India-specific effects |
| expert analysis | analysts, researchers, think tanks, explainers |
| primary sources | official statements and documents |
| criticism and counterarguments | critics, opposing arguments, risks |
| open questions | what is uncertain or unresolved |

Searches are budgeted by cost. Local providers run one broad query; GDELT runs at most
`gdelt.max_queries_per_event` combined queries (facts; analysis/opinion; India via
`sourcecountry:IN`; business; official domains); Guardian runs two. Each result is then labelled
with the questions it actually serves, from its own text, kind and source type. A question with
no selected source is listed as a limitation ("Nothing found for: criticism…").

## Search providers

| Provider | Type | Cost / limits | Notes |
|---|---|---|---|
| `archive` | IntelPod's own stored articles | free, local | grows with every daily run |
| `feeds` | `config/research_feeds.yaml` (official bodies, think tanks) | free public feeds | fetched once per run; items kept `feeds.keep_days` |
| `gdelt` | GDELT DOC 2.0 with `startdatetime`/`enddatetime` | free public service, throttled | metadata only; "first seen" dates; attribution required |
| `guardian` | The Guardian Content API | free developer key, non-commercial | optional (`GUARDIAN_API_KEY`); metadata + trail text only |

Not used, on purpose: Brave Search (no free plan for new users, card required, terms forbid
storing results), SearXNG and DuckDuckGo/“ddgs” scrapers (undocumented interfaces, CAPTCHA
solving), Google Custom Search (closed to new customers) and Bing Search APIs (retired). The New
York Times Article Search API is not wired in; its terms exclude using content for AI systems,
and `nytimes.com` pages are kept to metadata only by default (`pages.no_extract_domains`).

## Pages: what is fetched and what is kept

A result page is fetched only if every check passes:

* http(s) on a public host: no localhost, and no name whose DNS answer includes any private,
  loopback, link-local or reserved address — re-checked after each redirect. (The HTTP library
  resolves the name again when connecting; a malicious DNS server could still answer differently
  the second time. IntelPod only fetches URLs that news providers returned for public articles.)
* robots.txt allows it (for the page and every redirect target). Per RFC 9309 a robots.txt that
  answers 401/403/5xx or cannot be reached means "disallow everything"; a robots.txt redirect is
  followed only within the same site;
* at most `pages.max_bytes`, `pages.max_pages_per_event`, `pages.max_pages_per_run`; each host
  is throttled by the shared HTTP client.

Then only **metadata** is used (no extracts, no model) when the page is not HTML, the publisher
marks it not free (`isAccessibleForFree: false`), it is a login/subscription wall (HTTP
401/402/403 with wall text, or a redirect to a login page), it carries a `noai`, `noarchive`,
`nosnippet`, `max-snippet:0` or `none` robots directive (meta tag or `X-Robots-Tag`), it reserves text-and-data
mining (`tdm-reservation: 1`), or its domain is in `pages.no_extract_domains`.

From readable pages IntelPod keeps up to `max_extracts_per_result` **verbatim sentences** of at
most `max_extract_chars` characters that mention the event's names and terms, and up to three
attributed statements ("X said …") — in total never more than a quarter of the page's text and
never more than 900 characters, so a short release is never stored whole. The page text itself stays in memory for the current run and
is never stored (`PageInfo.text` is excluded from every serialisation); a test checks that no
stored row contains article body text. Page metadata (never text) is cached `pages.cache_days`
so blocked or paywalled pages are not requested again every day.

## Dates: publication vs event

Each source keeps `published_at` with where it came from (`page_metadata` > `feed` > `api` >
`gdelt_seendate` > `missing`), notes on conflicts (page and provider dates more than three days
apart), and `event_timing` relative to the event's first report: `before_event` (background),
`during_event`, `after_event` or `undated`. Future and implausible dates are rejected and noted.
Sources older than the window but newer than `background_days` are kept only as labelled
background (or dropped when `allow_background: false`); older ones are dropped. Events skipped
for the time budget, or whose research failed, are researched again on resume.

## Relevance and quality

**Relevance** (0–1) uses the clustering text pipeline (country tokens, synonyms, stemming) with
TF-IDF over the event's own reports and all its candidates, plus a bonus for the event's named
entities. Calibrated on the labelled corpus (`tests/fixtures/clustering_corpus.json`): at the
default 0.12 it keeps 89% of same-story items with precision 0.67; at 0.15, 79% / 0.77.

**Quality** is a weighted mean of the components IntelPod can actually observe; each has its
evidence, and unknown ones are left out and listed:

| Component | Basis |
|---|---|
| proximity | primary 1.0, research 0.8, reporting 0.7, analysis 0.6, opinion 0.5 |
| transparency | named author, exact publication date, identified publisher, corrections/standards link |
| evidence | links to primary documents, figures and attributed statements in the extracts |
| independence | lower when the same publisher already supplies other results; a headline carried by many outlets is kept once (the earliest outlet) and counted once |
| recency | newer within the window; background 0.3 |
| relevance | the relevance score |
| expertise | only where evident (e.g. a central bank on monetary policy, a think tank) |
| track record, corrections policy | **unknown** until Phase 3's source registry provides documented evidence |

There is deliberately no "famous publisher" component, and repetition never counts as
corroboration. Flags explain special cases: `interested_party` (an official voice of a party to
the event: evidence of its own position, not neutral verification), `opinion`,
`widely_carried`, `paywalled`, `metadata_only`, `date_approximate`, `background`.

## Selection

A deterministic greedy selection maximises relevance × quality with bonuses for covering a new
research question, a new kind of source and a new publisher, capped by `max_per_publisher` and
`max_from_event`, skipping near-duplicate headlines. Every assessed source keeps a
`selection_reason` ("selected: adds criticism…", "not used: relevance 0.08 below 0.12", …).

## Optional model help (NVIDIA or Ollama)

At most two calls per event, cached, bounded by `max_model_calls`:

* `research_queries`: extra search keywords (sanitised: letters, digits, spaces, hyphens; no
  operators).
* `research_notes`: one neutral note per selected source, written only from the title and the
  extracts IntelPod already took (rejected if it names a person, organisation or number absent from
  them; shown as "Note (model…)"), and pointers to attributed statements, which are accepted only
  if they appear **word for word** in the extracts — a model can never rephrase who said what. The model may
  flag a source as off-topic, but not more than a third of the package.

Without a key, or if the provider fails, research runs fully deterministically and says so.

## Storage

Tables (schema v3): `research_items` (research-feed items), `research_queries`,
`research_results` (every assessed source per run and event, selected or not, with reasons),
`research_packages` (per run and event: coverage, content mix, limitations, stats),
`search_cache` and `page_cache`. The report adds a "Research" block per event and writes
`data/runs/<run_id>/research.json`.

## Limits of Phase 2

* Discovery depends on what GDELT, the configured feeds and (optionally) the Guardian index;
  there is no general web search (no free, permitted one exists).
* GDELT dates are "first seen" times until a page's own metadata replaces them.
* Many publishers block automated access or are paywalled: those sources contribute headline and
  date only, and the package says how many.
* Interpretive balance (viewpoint diversity, claim-level evidence maps, disagreement analysis) is
  Phase 3.


## Relevance anchor (added after the first live runs)

Similarity alone let long unrelated articles through on shared common words and a shared country
(a US midterms story attached to a US sanctions event). A result must now name one of the event's
actors, one of its four most distinctive terms, or two of its top ten, in its title, snippet or
extracts; otherwise its relevance is multiplied by 0.4 and the reason is shown. On the labelled
corpus at the default threshold (0.12) precision rose from 0.67 to 0.78 and recall went from
0.89 to 0.78 (F1 0.76 → 0.78); off-topic matches roughly halved.
