"""Replaceable search providers for research.

Every provider implements `search(query) -> SearchResponse` and never raises for a failed
request: the response carries the status ("ok", "empty", "rate_limited", "error",
"skipped") and the error, so one broken source cannot sink research.

* `LocalSearch` ("archive", "feeds"): IntelPod's own stored news articles and the items of
  research feeds (official bodies, international organisations, think tanks). Free and
  instant; it grows with every daily run, so "recent history" improves over time.
* `GdeltSearch` ("gdelt"): GDELT DOC 2.0 article search over an explicit date window
  (`startdatetime`/`enddatetime`, last ~3 months only). Metadata only; dates are "first seen".
* `CachedSearch`: wraps a network provider with the SQLite search cache.
"""
from __future__ import annotations

import abc
import hashlib
import json
import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from ..config import GdeltProviderConfig, ResearchGdeltConfig
from ..http import FetchError, HttpClient, _retry_after_seconds
from ..providers.base import FetchRecord
from ..providers.gdelt import (GDELT_DOC_URL, GDELT_HOST, RATE_LIMIT_HINTS, GdeltError, _LANG, gdelt_title,
                               parse_artlist, parse_seendate)
from ..textutil import (Taxonomy, canonicalize_url, domain_of, fold_countries, strip_html,
                        tokenize)
from .models import ResearchDateSource, ResearchQuery, SearchHit, SearchResponse

log = logging.getLogger(__name__)

_DATE_SOURCE_MAP: dict[str, ResearchDateSource] = {"feed": "feed", "api": "api", "gdelt_seendate": "gdelt_seendate"}


class SearchProvider(abc.ABC):
    name: str = "base"
    network: bool = False  # True when a search costs a request to an outside service

    @abc.abstractmethod
    def search(self, query: ResearchQuery) -> SearchResponse:
        """Run one query. Must not raise for request failures."""

    def note(self) -> str | None:
        """A one-line status for the research summary (e.g. why the provider stopped)."""
        return None


# -- local search -------------------------------------------------------------------------
@dataclass
class LocalDoc:
    url: str
    title: str
    description: str | None
    publisher: str
    domain: str | None
    published_at: datetime | None
    date_source: ResearchDateSource
    source_type: str | None
    country: str | None
    language: str | None
    effective_time: datetime


class LocalIndex:
    """Small inverted index over titles + descriptions (same tokenizer as clustering, so
    "Kremlin"/"Russian" and "curbs"/"restrictions" match)."""

    def __init__(self, docs: list[LocalDoc], taxonomy: Taxonomy):
        self.docs = docs
        self.taxonomy = taxonomy
        self.postings: dict[str, set[int]] = defaultdict(set)
        self.title_tokens: list[set[str]] = []
        for i, d in enumerate(docs):
            title = set(self.tokens(d.title))
            self.title_tokens.append(title)
            for t in title | set(self.tokens(d.description or "")):
                self.postings[t].add(i)
        n = len(docs)
        self.idf = {t: math.log((1 + n) / (1 + len(ix))) + 1 for t, ix in self.postings.items()}

    def tokens(self, text: str) -> list[str]:
        return tokenize(fold_countries(self.taxonomy.fold_phrases(text)), self.taxonomy.synonyms)

    def search(self, terms: Iterable[str], start: datetime, end: datetime, limit: int = 25,
               min_score: float = 0.3, min_matched: int = 2) -> list[tuple[LocalDoc, float, list[str]]]:
        """Documents in [start, end] matching the query terms, scored by the IDF-weighted share of
        query tokens they contain (title matches count 1.5x). Requires `min_matched` distinct
        tokens so one generic word ("India", "rates") never matches on its own."""
        q = list(dict.fromkeys(t for term in terms for t in self.tokens(term)))
        if not q:
            return []
        total = sum(self.idf.get(t, math.log(1 + len(self.docs)) + 1) for t in q)
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, list[str]] = defaultdict(list)
        for t in q:
            for i in self.postings.get(t, ()):
                scores[i] += self.idf[t] * (1.5 if t in self.title_tokens[i] else 1.0)
                matched[i].append(t)
        out = []
        for i, sc in scores.items():
            d = self.docs[i]
            if not (start <= d.effective_time <= end) or len(matched[i]) < min(min_matched, len(q)):
                continue
            score = min(1.0, sc / total)
            if score >= min_score:
                out.append((d, round(score, 3), matched[i]))
        out.sort(key=lambda x: (-x[1], -x[0].effective_time.timestamp(), x[0].url))
        return out[:limit]


class LocalSearch(SearchProvider):
    """Searches documents supplied by `loader(start, end)`; indexes are built once per window."""

    network = False

    def __init__(self, name: str, loader: Callable[[datetime, datetime], list[LocalDoc]], taxonomy: Taxonomy,
                 limit: int = 25):
        self.name = name
        self.loader = loader
        self.taxonomy = taxonomy
        self.limit = limit
        self._indexes: dict[tuple[datetime, datetime], LocalIndex] = {}

    def _index(self, start: datetime, end: datetime) -> LocalIndex:
        key = (start.replace(minute=0, second=0, microsecond=0), end.replace(minute=0, second=0, microsecond=0))
        if key not in self._indexes:
            self._indexes[key] = LocalIndex(self.loader(key[0], key[1] + timedelta(hours=1)), self.taxonomy)
        return self._indexes[key]

    def search(self, query: ResearchQuery) -> SearchResponse:
        try:
            found = self._index(query.start, query.end).search(query.terms or [query.text], query.start, query.end,
                                                               limit=self.limit)
        except Exception as e:  # noqa: BLE001 - a local index problem must not stop research
            log.exception("%s search failed", self.name)
            return SearchResponse(provider=self.name, query_id=query.query_id, status="error", error=str(e)[:300])
        hits = [SearchHit(provider=self.name, query_id=query.query_id, query_type=query.query_type, url=d.url,
                          title=d.title, publisher=d.publisher, domain=d.domain, published_at=d.published_at,
                          date_source=d.date_source, snippet=d.description, language=d.language,
                          source_country=d.country, source_type=d.source_type)
                for d, _score, _m in found]
        return SearchResponse(provider=self.name, query_id=query.query_id, status="ok" if hits else "empty", hits=hits)


def article_docs(articles) -> list[LocalDoc]:
    """LocalDocs from Phase 1 `Article`s (the archive of everything IntelPod ingested)."""
    return [LocalDoc(url=a.source_url, title=a.title, description=a.description, publisher=a.source_name,
                     domain=a.source_domain, published_at=a.published_at,
                     date_source=_DATE_SOURCE_MAP.get(a.date_source, "missing") if a.published_at else "missing",
                     source_type=a.source_type or "news", country=a.country, language=a.language,
                     effective_time=a.effective_time) for a in articles]


def research_item_docs(rows) -> list[LocalDoc]:
    """LocalDocs from stored research-feed items."""
    return [LocalDoc(url=r.url, title=r.title, description=r.description, publisher=r.publisher, domain=r.domain,
                     published_at=r.published_at, date_source="feed" if r.published_at else "missing",
                     source_type=r.source_type, country=r.country, language="en",
                     effective_time=r.published_at or r.discovered_at) for r in rows]


# -- GDELT ------------------------------------------------------------------------------------
def gdelt_datetime(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


class GdeltSearch(SearchProvider):
    """GDELT DOC 2.0 ArtList over the query's [start, end] window.

    Shares GDELT's politeness rules with ingestion: requests at least `min_interval_seconds`
    apart; after a refusal (HTTP 429 or the plain-text "Please limit requests" notice) one retry
    after a cool-down that doubles each time; after `stop_after_refusals` refused queries, or when
    `max_requests_per_run` is spent, further queries are skipped with a note instead of waiting."""

    name = "gdelt"
    network = True

    def __init__(self, http: HttpClient, cfg: ResearchGdeltConfig, polite: GdeltProviderConfig,
                 domain_publishers: dict[str, str] | None = None, sleep=None):
        self.http = http
        self.cfg = cfg
        self.polite = polite
        self.domain_publishers = domain_publishers or {}
        self._sleep = sleep or http._sleep
        self.requests = 0
        self.refused = 0
        self._cooldown = polite.cooldown_seconds
        self.stopped: str | None = None
        self.fetches: list[FetchRecord] = []
        http.set_host_interval(GDELT_HOST, polite.min_interval_seconds)

    def params(self, query: ResearchQuery) -> dict[str, str]:
        # Newest first for "latest facts"; GDELT's relevance sort for everything else.
        sort = "DateDesc" if query.query_type == "latest_facts" else self.cfg.sort
        return {"query": query.text, "mode": "ArtList", "format": "json", "maxrecords": str(self.cfg.max_records),
                "sort": sort, "startdatetime": gdelt_datetime(query.start),
                "enddatetime": gdelt_datetime(query.end)}

    def note(self) -> str | None:
        return self.stopped

    def search(self, query: ResearchQuery) -> SearchResponse:
        if self.stopped:
            return SearchResponse(provider=self.name, query_id=query.query_id, status="skipped", error=self.stopped)
        problems = gdelt_query_problems(query.text)
        if problems:
            # GDELT answers malformed queries with an error text after a slow round trip; never send them.
            return SearchResponse(provider=self.name, query_id=query.query_id, status="error",
                                  error="query not sent (invalid for GDELT): " + "; ".join(problems))
        if self.requests >= self.cfg.max_requests_per_run:
            self.stopped = f"GDELT research budget of {self.cfg.max_requests_per_run} requests per run used up"
            return SearchResponse(provider=self.name, query_id=query.query_id, status="skipped", error=self.stopped)
        hits, err, wait, made = self._once(query)
        if wait is not None and wait > self.polite.max_cooldown_seconds:
            self.stopped = "GDELT asked to wait longer than max_cooldown_seconds; remaining GDELT research queries skipped"
            return SearchResponse(provider=self.name, query_id=query.query_id, status="rate_limited",
                                  requests=made, error=err)
        if wait is not None:
            pause = max(wait, self._cooldown)
            log.info("GDELT refused a research query; cooling down %.0f s before one retry", pause)
            self._sleep(pause)
            self._cooldown = min(self._cooldown * 2, self.polite.max_cooldown_seconds)
            hits, err, wait, more = self._once(query)
            made += more
            if wait is not None:
                self.refused += 1
                if self.refused >= self.polite.stop_after_refusals:
                    self.stopped = "GDELT kept refusing requests (rate limit); remaining GDELT research queries skipped"
                else:
                    self._sleep(self._cooldown)
                return SearchResponse(provider=self.name, query_id=query.query_id, status="rate_limited",
                                      requests=made, error=err)
        if err is not None:
            return SearchResponse(provider=self.name, query_id=query.query_id, status="error", requests=made, error=err)
        return SearchResponse(provider=self.name, query_id=query.query_id, status="ok" if hits else "empty",
                              hits=hits, requests=made)

    def _once(self, query: ResearchQuery) -> tuple[list[SearchHit], str | None, float | None, int]:
        """One request: (hits, error, wait-if-refused, requests made)."""
        params = self.params(query)
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        self.requests += 1

        def record(ok: bool, status: int | None = None, error: str | None = None, items: int = 0) -> None:
            self.fetches.append(FetchRecord(provider="gdelt_research", target=f"query={params['query']}",
                                            started_at=started, duration_ms=int((time.monotonic() - t0) * 1000),
                                            ok=ok, status_code=status, item_count=items, error=error))

        try:
            resp = self.http.request("GET", GDELT_DOC_URL, params=params, check_robots=False,
                                     retry_statuses={500, 502, 503, 504})
        except FetchError as e:
            record(False, e.status_code, str(e))
            return [], str(e), None, 1
        if resp.status_code == 429:
            record(False, 429, "HTTP 429: rate limited by GDELT")
            return [], "HTTP 429: rate limited by GDELT", _retry_after_seconds(resp) or 0.0, 1
        if resp.status_code >= 400:
            record(False, resp.status_code, f"HTTP {resp.status_code}")
            return [], f"HTTP {resp.status_code}", None, 1
        try:
            rows = parse_artlist(resp.text)
        except GdeltError as e:
            limited = any(h in str(e).lower() for h in RATE_LIMIT_HINTS)
            record(False, resp.status_code, str(e))
            return [], str(e), (0.0 if limited else None), 1
        hits = [h for row in rows if (h := _safe(self._to_hit, row, query)) is not None]
        record(True, resp.status_code, items=len(hits))
        return hits, None, None, 1

    def _to_hit(self, row: dict, query: ResearchQuery) -> SearchHit | None:
        url = (row.get("url") or "").strip()
        domain = (row.get("domain") or "").lower().removeprefix("www.") or domain_of(url)
        title = gdelt_title(row.get("title") or "", self._publisher_for(domain), domain)
        if not url.startswith(("http://", "https://")) or not title:
            return None
        seen = parse_seendate(row.get("seendate"))
        lang = (row.get("language") or "").strip().lower()
        return SearchHit(provider=self.name, query_id=query.query_id, query_type=query.query_type, url=url, title=title,
                         publisher=self._publisher_for(domain), domain=domain, published_at=seen,
                         date_source="gdelt_seendate" if seen else "missing",
                         language=_LANG.get(lang, lang[:2] or None), source_country=row.get("sourcecountry") or None,
                         source_type="news")

    def _publisher_for(self, domain: str | None) -> str | None:
        if not domain:
            return None
        for known, publisher in self.domain_publishers.items():
            if domain == known or domain.endswith("." + known):
                return publisher
        return domain


GDELT_MAX_QUERY_CHARS = 200
_GDELT_OPERATOR = re.compile(r"^-?(sourcelang|sourcecountry|domain|domainis|theme|tone|toneabs|near\d+|repeat\d+):", re.I)


def gdelt_query_problems(q: str) -> list[str]:
    """Rules GDELT enforces (observed; see docs/PROVIDERS.md): at most ~200 characters; OR lists
    in parentheses, never nested; parentheses only around OR lists; quotes only around phrases of
    two or more words; no bare word of 2 characters or fewer."""
    problems = []
    if len(q) > GDELT_MAX_QUERY_CHARS:
        problems.append(f"longer than {GDELT_MAX_QUERY_CHARS} characters")
    if q.count('"') % 2:
        problems.append("unbalanced quotes")
    depth = 0
    for ch in re.sub(r'"[^"]*"', "", q):
        depth += ch == "("
        depth -= ch == ")"
        if depth > 1 or depth < 0:
            problems.append("nested or unbalanced parentheses")
            break
    if depth not in (0,) and "nested or unbalanced parentheses" not in problems:
        problems.append("nested or unbalanced parentheses")
    for group in re.findall(r"\(([^()]*)\)", re.sub(r'"[^"]*"', '"x"', q)):
        if " OR " not in group:
            problems.append("parentheses around something other than an OR list")
            break
    if re.search(r"(?<!\()\b\S+ OR \S+\b(?![^(]*\))", re.sub(r'"[^"]*"', "x", q)):
        problems.append("OR list without parentheses")
    for phrase in re.findall(r'"([^"]*)"', q):
        if len(phrase.split()) < 2:
            problems.append(f'single word in quotes: "{phrase}"')
    bare = re.sub(r'"[^"]*"', " ", q).replace("(", " ").replace(")", " ")
    for tok in bare.split():
        if tok in ("OR", "AND") or _GDELT_OPERATOR.match(tok):
            continue
        word = tok.lstrip("-")
        if len(word) <= 2:
            problems.append(f"word too short for GDELT: {tok}")
    return list(dict.fromkeys(problems))


# -- Guardian ----------------------------------------------------------------------------------------
GUARDIAN_SEARCH_URL = "https://content.guardianapis.com/search"
GUARDIAN_HOST = "content.guardianapis.com"


class GuardianSearch(SearchProvider):
    """The Guardian Open Platform Content API (free developer key, non-commercial use).

    Exact date windows (`from-date`/`to-date`, `use-date=published`). IntelPod asks only for
    metadata fields (headline, trail text, byline, first publication date) and never for the body.
    The trail text is used to judge relevance and is kept only as a short snippet. The key travels
    as the `api-key` query parameter; IntelPod never logs query strings."""

    name = "guardian"
    network = True

    def __init__(self, http: HttpClient, api_key: str | None, max_requests: int, page_size: int = 20,
                 min_interval_seconds: float = 1.1):
        self.http = http
        self.api_key = api_key
        self.max_requests = max_requests
        self.page_size = page_size
        self.requests = 0
        self.stopped: str | None = None if api_key else "GUARDIAN_API_KEY not set"
        self.fetches: list[FetchRecord] = []
        http.set_host_interval(GUARDIAN_HOST, min_interval_seconds)

    def note(self) -> str | None:
        return self.stopped

    def search(self, query: ResearchQuery) -> SearchResponse:
        if self.stopped:
            return SearchResponse(provider=self.name, query_id=query.query_id, status="skipped", error=self.stopped)
        if self.requests >= self.max_requests:
            self.stopped = f"Guardian budget of {self.max_requests} requests per run used up"
            return SearchResponse(provider=self.name, query_id=query.query_id, status="skipped", error=self.stopped)
        params = {"q": query.text, "from-date": query.start.date().isoformat(), "to-date": query.end.date().isoformat(),
                  "use-date": "published", "order-by": "newest" if query.query_type == "latest_facts" else "relevance",
                  "page-size": str(self.page_size), "show-fields": "headline,trailText,byline,firstPublicationDate",
                  "api-key": self.api_key}
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        self.requests += 1

        def record(ok: bool, status: int | None = None, error: str | None = None, items: int = 0) -> None:
            self.fetches.append(FetchRecord(provider="guardian", target=f"q={query.text}", started_at=started,
                                            duration_ms=int((time.monotonic() - t0) * 1000), ok=ok,
                                            status_code=status, item_count=items, error=error))

        try:
            resp = self.http.request("GET", GUARDIAN_SEARCH_URL, params=params, check_robots=False,
                                     retry_statuses={500, 502, 503, 504})
        except FetchError as e:
            record(False, e.status_code, str(e))
            return SearchResponse(provider=self.name, query_id=query.query_id, status="error", requests=1, error=str(e))
        if resp.status_code in (401, 403):
            self.stopped = f"Guardian rejected the key (HTTP {resp.status_code})"
            record(False, resp.status_code, self.stopped)
            return SearchResponse(provider=self.name, query_id=query.query_id, status="error", requests=1,
                                  error=self.stopped)
        if resp.status_code == 429:
            self.stopped = "Guardian rate limit reached (HTTP 429); remaining Guardian queries skipped"
            record(False, 429, self.stopped)
            return SearchResponse(provider=self.name, query_id=query.query_id, status="rate_limited", requests=1,
                                  error=self.stopped)
        if resp.status_code >= 400:
            record(False, resp.status_code, f"HTTP {resp.status_code}")
            return SearchResponse(provider=self.name, query_id=query.query_id, status="error", requests=1,
                                  error=f"HTTP {resp.status_code}")
        try:
            rows = (resp.json().get("response") or {}).get("results") or []
        except (ValueError, AttributeError) as e:
            record(False, resp.status_code, f"malformed JSON: {e}")
            return SearchResponse(provider=self.name, query_id=query.query_id, status="error", requests=1,
                                  error=f"malformed JSON: {e}")
        hits = [h for row in rows if isinstance(row, dict) and (h := _safe(self._to_hit, row, query)) is not None]
        record(True, resp.status_code, items=len(hits))
        return SearchResponse(provider=self.name, query_id=query.query_id, status="ok" if hits else "empty",
                              hits=hits, requests=1)

    def _to_hit(self, row: dict, query: ResearchQuery) -> SearchHit | None:
        url = (row.get("webUrl") or "").strip()
        fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        title = strip_html(fields.get("headline") or row.get("webTitle") or "")
        if not url.startswith("https://") or not title:
            return None
        published = _parse_iso(fields.get("firstPublicationDate") or row.get("webPublicationDate"))
        snippet = strip_html(fields.get("trailText") or "")[:300] or None
        return SearchHit(provider=self.name, query_id=query.query_id, query_type=query.query_type, url=url, title=title,
                         publisher="The Guardian", domain="theguardian.com", published_at=published,
                         date_source="api" if published else "missing", snippet=snippet, language="en",
                         source_country="GB", source_type="news", section=row.get("sectionId") or None)


def _safe(fn, row, query):
    """Convert one provider row; a malformed row is skipped, never fatal."""
    try:
        return fn(row, query)
    except Exception:  # noqa: BLE001 - external data
        log.debug("skipping malformed result row: %r", row)
        return None


def _parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# -- cache --------------------------------------------------------------------------------------
def search_cache_key(provider: str, query: ResearchQuery) -> str:
    """Same provider + query text + window (to the hour) -> same key."""
    hour = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%d%H")  # noqa: E731
    raw = json.dumps([provider, query.text, sorted(query.terms), hour(query.start), hour(query.end)])
    return hashlib.sha256(raw.encode()).hexdigest()


class CachedSearch(SearchProvider):
    """Answer repeated network queries from the search cache (resumed runs never pay twice)."""

    def __init__(self, inner: SearchProvider, store, max_age: timedelta):
        self.inner = inner
        self.store = store
        self.max_age = max_age
        self.name = inner.name
        self.network = inner.network

    def note(self) -> str | None:
        return self.inner.note()

    def search(self, query: ResearchQuery) -> SearchResponse:
        key = search_cache_key(self.name, query)
        if self.max_age.total_seconds() > 0:
            cached = self.store.search_cache_get(key, self.max_age)
            if cached is not None:
                hits = [h.model_copy(update={"query_id": query.query_id, "query_type": query.query_type}) for h in cached]
                return SearchResponse(provider=self.name, query_id=query.query_id, status="cached", hits=hits)
        resp = self.inner.search(query)
        if resp.status in ("ok", "empty") and self.max_age.total_seconds() > 0:
            self.store.search_cache_put(key, self.name, resp.hits)
        return resp


def canonical(url: str) -> str:
    try:
        return canonicalize_url(url)
    except ValueError:
        return url
