# IntelPod Phase 2 (internet research) — module spec for implementers

Repository: /home/user/News (Python 3.13, `.venv/bin/python`, tests `.venv/bin/python -m pytest -q`).
Read first: `intelpod/research/models.py` (the data contracts — do NOT change field names; if you
need a new field, add it with a default and say so in your report), `intelpod/config.py`
(`ResearchConfig` and sub-configs), `intelpod/textutil.py`, `intelpod/http.py`,
`intelpod/pipeline/cluster.py` (tokenizing, TF-IDF, `combine_similarity`, `build_docs`),
`intelpod/llm/parsing.py` (`unsupported_terms` grounding check), `tests/conftest.py`.

Product rules that every module must respect (from the user's spec):
* Never bypass paywalls, logins, CAPTCHAs, robots.txt or rate limits. Documented interfaces only.
* Never store or reproduce full articles: metadata + short extracts (<= `max_extract_chars`,
  at most `max_extracts_per_result` per result) + IntelPod's own notes. Full text stays in memory.
* Never invent sources, URLs, quotes, dates or statistics. If evidence is missing, say so.
* Distinguish an item's publication date from the date of the event it discusses.
* Do not treat a famous publisher as automatically correct; repetition is not corroboration.

Code style: small typed functions, docstrings that explain *why*, no placeholder code, no
network in unit tests (mock httpx with `respx` or `httpx.MockTransport`), deterministic tests.
Keep comments short and useful. Follow the existing code style (see cluster.py / rank.py).

## Module group A — `intelpod/research/pages.py` and `intelpod/research/evidence.py`

### pages.py
`class PageFetcher(http: HttpClient, cfg: ResearchPagesConfig, resolver=socket.getaddrinfo)`
with `fetch(url: str) -> PageInfo` that NEVER raises (status + error explain failures):
1. URL safety: only http/https; reject (status `blocked_host`) hosts that are `localhost`, have no
   dot, are IP literals in private/loopback/link-local/reserved/multicast ranges, or that resolve
   (via `resolver`) only to such addresses. If DNS resolution fails, allow (an HTTP proxy may resolve
   it) — the fetch itself will fail if unreachable. Re-check the final URL after redirects too.
2. robots.txt: use `HttpClient.allowed_by_robots(url)`; disallowed -> `robots_disallowed`, no request.
3. Fetch with a size cap: add a method to `HttpClient` (in intelpod/http.py), e.g.
   `get_limited(url, max_bytes, accept=...) -> (response_meta, body_bytes, truncated)` that streams
   (`self._client.stream`), honours the same per-host throttle (`_throttle`) and robots check,
   follows redirects, stops reading after `max_bytes`, retries only connection errors/timeouts/5xx
   like `request()` does, and never logs query strings. Keep `request()` behaviour unchanged.
4. Status mapping: 401/402/403 -> `paywalled` if the body/headers suggest a login/subscription
   wall, else `error`; other 4xx/5xx -> `error` (keep http_status); final URL path containing
   `/login`, `/signin`, `/subscribe`, `/paywall`, `/register` -> `paywalled`; non-HTML content types
   (application/pdf etc.) -> `metadata_only` (record content type in `error`/notes); body larger than
   `max_bytes` -> parse what was read if it is HTML, but mark text as partial? (choose: extract from
   the truncated HTML but never fail hard); timeouts -> `error`.
5. Parse HTML: JSON-LD (`<script type="application/ld+json">`, may be a list or contain `@graph`)
   for `@type` (NewsArticle, OpinionNewsArticle, AnalysisNewsArticle, ReportageNewsArticle,
   ScholarlyArticle, Report, PressRelease...), `datePublished`, `dateModified`, `isAccessibleForFree`
   (also nested `hasPart` with `isAccessibleForFree`), `author`, `publisher.name`, `headline`,
   `description`; meta tags `article:published_time`, `article:modified_time`, `og:title`,
   `og:description`, `og:site_name`, `author`, `description`, `<time datetime>` as a last resort.
   Robots directives from `<meta name="robots"|"googlebot"...>` and the `X-Robots-Tag` header:
   `noai`, `noimageai`, `noarchive`, `nosnippet`, `none` -> status `metadata_only` (no text kept,
   no extracts may be made); record which directive in `error`.
   `isAccessibleForFree == false` -> `paywalled` (metadata only).
6. Main text: use `trafilatura` (dependency `trafilatura>=2.0`, Apache-2.0; add it to pyproject.toml
   and requirements.txt) `bare_extraction(..., with_metadata=True, favor_precision=True)`; fall
   back to a simple built-in extractor (paragraph text from <p> under <article>/<main>) if the import
   fails. Use trafilatura's `pagetype` when JSON-LD has no @type. Fill `word_count`, `text` (memory
   only; field is excluded from serialisation), `outbound_primary_links` (count of links to .gov,
   .gov.xx, .gob, .gouv, .int, .mil, .edu, .ac.xx, known central-bank/international-org domains,
   or documents ending .pdf), `has_corrections_link` (a link whose text or href mentions
   corrections / editorial standards / ethics policy).
7. Dates: parse with timezone awareness (naive -> UTC); reject dates in the future (> now + 2h) or
   before 1995 (leave None, note why in `date_found_in`?). `published_at` = original publication date
   (datePublished / article:published_time), never dateModified. Set `date_found_in`.
8. Thread safety: `fetch` will be called from a small thread pool (different hosts in parallel).

### evidence.py
* `split_sentences(text: str) -> list[str]` — robust to abbreviations (U.S., Mr., Dr., Jan.,
  decimals like 3.25%, initials), quotes, and newlines.
* `extract_evidence(text, terms, entities, max_notes=3, max_chars=300) -> list[EvidenceNote]` —
  pick the sentences most relevant to the event: overlap with `terms` (stemmed via
  `textutil.tokenize`), named `entities`, plus small bonuses for numbers, attribution ("said",
  "according to", "told", "announced"), and position (earlier is better). Skip boilerplate
  (cookie/subscribe/newsletter/"read more"/"sign up"/very short or very long sentences).
  Notes are VERBATIM sentences (kind="extract"), trimmed at a word boundary with "…" if longer
  than `max_chars`. Never paraphrase. Deduplicate near-identical sentences.
* `extract_claims(text, terms, entities, max_claims=3, max_chars=300) -> list[str]` — attributed
  statements: sentences with an explicit source ("X said", "according to X", "X told", "X
  announced", quoted speech with attribution). Verbatim, trimmed as above, relevant to the terms.
* `snippet_notes(snippet, title, max_chars)` — when no page text is available, a provider snippet
  may become one note with source_field="snippet" (verbatim, trimmed).

### Tests (tests/test_research_pages.py, tests/test_research_evidence.py)
robots disallow (no page request made), private IP / localhost / non-http blocked, redirect to a
private address blocked, 403 subscription page -> paywalled, isAccessibleForFree=false -> paywalled,
noai / nosnippet meta and X-Robots-Tag -> metadata_only with no text, PDF -> metadata_only,
oversized body handled, timeout -> error, JSON-LD @graph parsing, OpinionNewsArticle type, date
precedence (datePublished over dateModified; meta over <time>), future date rejected, trafilatura
fallback path (monkeypatch the import), outbound primary link counting, corrections link; sentence
splitting edge cases, evidence selection ranks relevant sentences first, boilerplate skipped, length
limits, claims require attribution, everything verbatim (each note is a substring of the text,
modulo trimming).

## Module group C — analysis modules (pure functions, no network)

### `intelpod/research/queries.py`
* `event_keywords(brief: EventBrief, taxonomy) -> Keywords` (dataclass): `phrases` (1-3 most
  distinctive multi-word phrases from the headline/title, e.g. "general license", "repo rate"),
  `entities` (non-country named entities from brief.entities, original casing), `countries`,
  `terms` (top centroid terms mapped back to readable words where possible — centroid keys are
  stemmed/folded tokens like "ctryunitedstat" or "pair:..." which must NOT appear in queries),
  `topic`. Must handle events with very few terms.
* `build_queries(brief, taxonomy, cfg: ResearchConfig, now, providers: list[str],
  extra_terms: dict[str, list[str]] | None = None) -> list[ResearchQuery]`:
  - window: `start = now - window_days`, `end = now`; `latest_facts` uses `now - latest_days`.
  - For local providers ("archive", "feeds"): one query per each of the 10 query types (terms list;
    `text` = human-readable keywords).
  - For "gdelt": at most `cfg.gdelt.max_queries_per_event` COMBINED queries (each has `covers`):
    (1) core facts/history (latest_facts, recent_history), (2) core + analysis/opinion/expert/
    criticism cue words (expert_analysis, criticism, political_views, economic_views, open_questions),
    (3) India implications (india_impact; core + India terms, or `sourcecountry:IN` — check the
    research notes passed to you for the verified operator form), (4) business/economic consequences
    (business_impact, economic_views), (5) if budget allows, primary sources via official domain
    filters (primary_sources). Valid GDELT syntax: phrases in double quotes, OR groups inside
    parentheses, no words shorter than 3 characters, no bare stopwords, keep queries short
    (GDELT rejects overly long/complex queries), append `cfg.gdelt.language_filter`.
  - For "guardian" (and other plain keyword APIs): AND/OR boolean string per combined query
    (same grouping as gdelt), without GDELT-only operators.
  - `query_id` = `stable_hash(event_id, query_type, provider, text, start, end)`.
  - `extra_terms` (from an optional model call) add keywords per query type; sanitise them
    (letters/digits/spaces/hyphens/apostrophes only, 3-40 chars, max 4 per type, no operators).
* `CUE_WORDS: dict[QueryType, list[str]]` documented constants (e.g. criticism: critics,
  criticism, backlash, oppose, concern, risk; expert_analysis: analysts, experts, economists,
  analysis, commentary; open_questions: uncertain, unclear, outlook, "what next"...).

### `intelpod/research/classify.py`
* `classify_content(url, title, section, source_type, schema_type) -> tuple[ContentType, str]` —
  rules (first match wins, evidence string says which): source_type official/international_org ->
  primary; academic/think_tank -> research (or analysis for commentary paths); schema types
  (OpinionNewsArticle -> opinion, AnalysisNewsArticle -> analysis, ReportageNewsArticle ->
  reporting, ScholarlyArticle/Report -> research, PressRelease -> primary); URL path segments
  (/opinion/, /opinions/, /commentisfree/, /comment/, /editorial(s)/, /op-ed/, /columns?/,
  /blogs?/ -> opinion; /analysis/, /explainer(s)/, /explained/, /insight(s)/, /commentary/ ->
  analysis; /research/, /publications?/, /working-paper(s)/, /report(s)/ -> research;
  /press-release(s)/, /pressreleases?/, /newsroom/ -> primary); title cues ("Opinion:", "Op-ed",
  "Editorial:", "Analysis:", "Explained:", "Explainer", "What ... means", "Why ...") ; provider
  section; default "reporting" for news sources, "unknown" otherwise.
* `label_query_types(title, snippet, notes_text, content_type, source_type, countries,
  found_by: list[str]) -> list[QueryType]` — the query types a result actually serves: keep
  `found_by`, add types whose cue words appear in the text (criticism, expert_analysis,
  business_impact, economic_views, political_views, open_questions), `india_impact` if India terms
  appear (textutil country detection), `primary_sources` if content_type == primary.

### `intelpod/research/dates.py`
* `resolve_date(candidates: list[tuple[datetime | None, ResearchDateSource]], page: PageInfo | None,
  now, window_start, background_start, event_date) -> DateResolution` (dataclass with
  `published_at`, `date_source`, `notes: list[str]`, `in_window: bool`, `event_timing`, `keep: bool`).
  Precedence: page metadata (original publication) > feed > api > gdelt_seendate > missing.
  Reject (with a note) future dates (> now + 2h) and dates before 1995. If page date and a provider
  date differ by more than 3 days, keep the page date and note the conflict. `gdelt_seendate` alone
  is noted as approximate ("first seen by GDELT"). `in_window` = window_start <= date <= now + 2h.
  Older than window but newer than background_start -> keep=True, in_window=False (background).
  Older than background_start -> keep=False. Missing date -> keep=True, event_timing "undated".
  event_timing: before_event if date < event_date - 24h; during_event if within ±24h of event_date;
  after_event otherwise; "undated" if no date or no event_date.

### `intelpod/research/relevance.py`
* `class RelevanceModel(brief, taxonomy, cluster_cfg, corpus_texts: list[str], embedder=None)`
  with `score(title, text) -> tuple[float, str]` (0..1, evidence naming the matched terms/entities).
  Use the clustering tokenizer (country tokens, synonyms, stemming; see `build_docs`) and TF-IDF
  with IDF computed over `corpus_texts` (the event's own reports + all candidate texts of this
  event), event profile = the event's own report texts (title x2 + description) + brief.summary,
  plus a named-entity overlap bonus via `combine_similarity`. Optional embedder (callable
  texts -> vectors or None) may only RAISE the score (like clustering's boost mode), with the batch
  calibration idea from `dense_baseline` if you use it; keep it simple and tested.
  CALIBRATE `ResearchConfig.min_relevance` (default 0.12) on `tests/fixtures/clustering_corpus.json`:
  for each big/medium story, use its first 3 headlines as the "event", score all other headlines of
  the same day; same-story = relevant, others (especially `distractor` kind) = not relevant. Report
  precision/recall at a few thresholds and recommend a default; put a regression test on it.

### `intelpod/research/credibility.py`
* `assess(result: ResearchResult, page: PageInfo | None, brief: EventBrief, peers:
  list[ResearchResult]) -> CredibilityAssessment` with components (value None = unknown, excluded
  from the weighted mean and listed in `unknown`):
  - `proximity` (originality/proximity to evidence): primary/official 1.0, research/think tank 0.8,
    own reporting 0.7, analysis 0.6, opinion 0.5, syndicated copy 0.4 (`syndicated_with` non-empty
    and not the earliest), unknown type None.
  - `transparency`: share of [named author/byline, exact publication date (page_metadata/feed/api),
    identifiable publisher, corrections/standards link] that are present; None when the page was not
    fetched AND no metadata is available.
  - `evidence`: from page: outbound primary links, numbers/quotes in extracts; None if not fetched.
  - `independence`: 1.0 unique; lower when it repeats a headline carried by others; lower when
    the same publisher already supplies many results (peers).
  - `recency`: 1.0 inside the latest_days, decreasing across the window, 0.3 for background.
  - `relevance`: = result.relevance_score.
  - `expertise`: source_type vs topic (central bank/finance ministry for markets_monetary, trade
    ministry/WTO for trade_policy, think tank/academic for anything) -> 0.8-1.0; otherwise None.
  - `track_record` and `corrections_policy`: None (unknown) in Phase 2 — Phase 3's source registry
    supplies evidence-backed values. Never derive them from the publisher's fame.
  - Flags: `syndicated_copy`, `interested_party` (official source of a country/organisation that is a
    party to the event, e.g. the Kremlin on a Russia story — still primary evidence for what that
    party says, not neutral verification), `opinion` (value judgement content), `metadata_only`,
    `paywalled`, `date_approximate`, `background`.
  - `summary`: one short plain-English line.

### `intelpod/research/select.py`
* `dedupe_hits(hits: list[SearchHit], event_urls: set[str]) -> list[Candidate]` — merge by
  canonical URL (collect providers, query types, best date/snippet), mark `from_event`; group
  identical normalised titles across publishers (syndication: keep the earliest as representative,
  list the others in `syndicated_with`); drop near-identical titles (RapidFuzz token_sort_ratio >= 92)
  from the same publisher.
* `select_results(results: list[ResearchResult], cfg: ResearchConfig) -> list[ResearchResult]` —
  greedy, deterministic selection of up to `max_results`: value = relevance * (0.5 + 0.5 *
  credibility_or_0.5) with bonuses for covering a not-yet-covered query type (+0.15) and content
  type (+0.08) and a new publisher (+0.05); caps `max_per_publisher`, `max_from_event`; skip results
  below `min_relevance`, `keep=False` dates, and near-duplicates of a selected one; set `selected`
  and a human-readable `selection_reason` on EVERY result (why chosen / why not).
* `package_limitations(selected, all_results, queries: list[QueryStatus], cfg, notes: list[str]) ->
  list[str]` — explicit, honest limitations: fewer than `min_results`; query types with no selected
  result ("No criticism or counterarguments found"); no primary source; all results from one country
  or one content type; pages paywalled/robots-blocked counts; providers that failed or were rate
  limited (from notes); share of approximate/undated dates.

### Tests (tests/test_research_queries.py, ..._classify.py, ..._dates.py, ..._relevance.py,
..._credibility.py, ..._select.py)
Cover normal and edge cases listed above; GDELT query syntax validity (balanced quotes/parentheses,
no tokens < 3 chars, no internal tokens like "ctry..." or "pair:"), relevance calibration test on
the corpus fixture, selection caps/diversity/limitations, credibility unknown handling.
