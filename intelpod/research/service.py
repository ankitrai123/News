"""Phase 2 orchestration: research every selected event and store a research package.

For each event (highest ranked first, until `research.max_seconds` is spent):

 1. build queries: 10 query types, rendered per provider (template keywords, plus optional
    model-proposed keywords);
 2. search: local archive and research-feed items (free, instant), GDELT over the research
    window (throttled, budgeted, cached), optional keyed APIs;
 3. merge hits by canonical URL, detect syndicated copies, pre-score relevance from metadata;
 4. fetch the most promising pages (robots.txt, no private hosts, paywalls respected, size and
    count limits) for dates, page type and short verbatim extracts;
 5. resolve dates (publication vs event date), classify content type, score relevance and
    source quality, label the query types each source serves;
 6. optional grounded model notes; select 10-15 diverse, relevant sources; record honest
    limitations; persist results (selected and rejected, with reasons) and the package.

Nothing here raises for a provider or page failure; the package records what went wrong.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..config import Settings
from ..schemas import Article, EventCandidate, RankedEvent
from ..textutil import Taxonomy, canonicalize_url, domain_of, stable_hash
from . import model_assist
from .classify import classify_content, label_query_types
from .credibility import assess
from .dates import resolve_date
from .evidence import extract_claims, extract_evidence, snippet_notes
from .models import (QUERY_TYPES, Candidate, EventBrief, PageInfo, QueryStatus, ResearchPackage, ResearchQuery,
                     ResearchResult, SearchHit, SearchResponse)
from .providers import SearchProvider
from .queries import build_queries, event_keywords
from .relevance import RelevanceModel
from .select import dedupe_hits, package_limitations, select_results

log = logging.getLogger(__name__)

NOT_FETCHED_STATUSES_TO_CACHE = {"robots_disallowed", "paywalled", "blocked_host", "metadata_only"}


def make_brief(run_id: str, ranked: RankedEvent, cand: dict | EventCandidate | None,
               articles: list[Article]) -> EventBrief:
    c = cand.model_dump() if isinstance(cand, EventCandidate) else (cand or {})
    return EventBrief(
        event_id=ranked.event_id, run_id=run_id, rank=ranked.rank, title=ranked.title, headline=ranked.headline,
        summary=ranked.summary, description=ranked.description, topic=ranked.topic, subtopic=ranked.subtopic,
        countries=list(ranked.countries), entities=list(c.get("entities") or []), centroid=dict(c.get("centroid") or {}),
        first_article_at=ranked.first_article_at, latest_article_at=ranked.latest_article_at,
        article_ids=[a.article_id for a in articles], article_urls=[a.canonical_url for a in articles])


def own_report_hits(brief: EventBrief, articles: list[Article], limit: int = 8) -> list[SearchHit]:
    """The event's own Phase 1 reports are evidence too (latest facts); newest first."""
    out = []
    for a in sorted(articles, key=lambda x: x.effective_time, reverse=True)[:limit]:
        ds = {"feed": "feed", "api": "api", "gdelt_seendate": "gdelt_seendate"}.get(a.date_source, "missing")
        out.append(SearchHit(provider="event", query_id=f"event:{brief.event_id}", query_type="latest_facts",
                             url=a.source_url, title=a.title, publisher=a.source_name, domain=a.source_domain,
                             published_at=a.published_at, date_source=ds if a.published_at else "missing",
                             snippet=a.description, language=a.language, source_country=a.country,
                             source_type=a.source_type or "news", from_event=True))
    return out


class ResearchService:
    def __init__(self, settings: Settings, store, taxonomy: Taxonomy, providers: dict[str, SearchProvider],
                 page_fetcher=None, llm_client=None, embedder: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.cfg = settings.research
        self.store = store
        self.taxonomy = taxonomy
        self.providers = providers
        self.pages = page_fetcher
        self.llm = llm_client
        self.embedder = embedder
        self.clock = clock
        self.pages_fetched = 0
        self.page_stats: dict[str, int] = {}
        self.model_calls = 0
        self.model_errors: list[dict] = []
        self._model_dead = llm_client is None
        self._page_text: dict[str, PageInfo] = {}

    # -- run level ------------------------------------------------------------------------
    def research_events(self, run_id: str, events: list[tuple[EventBrief, list[Article]]], now: datetime,
                        skip_done: bool = True) -> dict:
        """Research events in the given order (best first). Returns stats for the stage output."""
        t0 = self.clock()
        done = self.store.researched_event_ids(run_id) if skip_done else set()
        per_event, skipped = [], []
        for i, (brief, articles) in enumerate(events[: self.cfg.max_events]):
            if brief.event_id in done:
                per_event.append({"event_id": brief.event_id, "status": "already_done"})
                continue
            if self.clock() - t0 > self.cfg.max_seconds:
                skipped.append(brief.event_id)
                self._save_skipped(run_id, brief, now, f"not researched: the run's research time budget "
                                                      f"({self.cfg.max_seconds:.0f} s) was used up")
                continue
            try:
                pkg = self.research_event(run_id, brief, articles, now)
                per_event.append({"event_id": brief.event_id, "status": pkg.status, "selected": len(pkg.result_ids),
                                  "candidates": pkg.candidate_count, "limitations": len(pkg.limitations)})
            except Exception as e:  # noqa: BLE001 - one event's failure must not stop the others
                log.exception("research for %s failed", brief.event_id)
                self._save_skipped(run_id, brief, now, f"research failed: {type(e).__name__}: {str(e)[:200]}",
                                   status="failed")
                per_event.append({"event_id": brief.event_id, "status": "failed", "error": str(e)[:300]})
        notes = {name: n for name, p in self.providers.items() if (n := p.note())}
        return {"events": per_event, "skipped_for_time": skipped, "pages_fetched": self.pages_fetched,
                "page_outcomes": dict(self.page_stats), "model_calls": self.model_calls,
                "model_errors": self.model_errors[:10], "provider_notes": notes,
                "seconds": round(self.clock() - t0, 1)}

    def _save_skipped(self, run_id: str, brief: EventBrief, now: datetime, reason: str, status: str = "skipped") -> None:
        start = now - timedelta(days=self.cfg.window_days)
        pkg = ResearchPackage(run_id=run_id, event_id=brief.event_id, event_title=brief.title, status=status,
                              window_start=start, window_end=now, event_date=brief.first_article_at,
                              limitations=[reason], created_at=datetime.now(timezone.utc))
        self.store.save_event_research(pkg, [], [])

    # -- event level ----------------------------------------------------------------------
    def research_event(self, run_id: str, brief: EventBrief, articles: list[Article], now: datetime) -> ResearchPackage:
        cfg = self.cfg
        window_start = now - timedelta(days=cfg.window_days)
        background_start = now - timedelta(days=max(cfg.background_days, cfg.window_days))
        notes: list[str] = []

        extra = self._model_keywords(brief, articles)
        queries = build_queries(brief, self.taxonomy, cfg, now, list(self.providers), extra_terms=extra)
        statuses, hits = self._search(queries)
        hits = own_report_hits(brief, articles) + hits
        event_urls = {u for u in brief.article_urls}
        candidates = dedupe_hits(hits, event_urls)

        # Relevance needs IDF statistics over this event's material: its own reports + candidates.
        event_texts = [f"{a.title} {a.title} {a.description or ''}" for a in articles]
        corpus = event_texts + [f"{c.title} {c.snippet or ''}" for c in candidates]
        rel = RelevanceModel(brief, self.taxonomy, self.settings.clustering, corpus, embedder=self.embedder,
                             event_texts=event_texts)
        for c in candidates:
            c.prescore = rel.score(c.title, c.snippet or "")[0]

        pages = self._fetch_pages(candidates)
        kw = event_keywords(brief, self.taxonomy)
        terms = list(dict.fromkeys(kw.phrases + kw.terms + [t for t in brief.centroid if t.isalpha()][:12]))
        entities = list(kw.entities)
        retrieved = datetime.now(timezone.utc)
        results: list[ResearchResult] = []
        dropped_dates = 0
        for c in candidates:
            r = self._build_result(brief, c, pages.get(c.canonical_url), now, window_start, background_start,
                                   rel, terms, entities, retrieved)
            if r is None:
                dropped_dates += 1
                continue
            results.append(r)
        for r in results:
            r.credibility_assessment = assess(r, pages.get(r.canonical_url), brief, results)

        selected = select_results(results, cfg)
        model_stats = self._model_notes(brief, selected)
        if model_stats and model_stats.get("off_topic"):
            off = set(model_stats["off_topic"])
            for r in results:
                if r.result_id in off:
                    r.relevance_score = 0.0
                    r.relevance_evidence = "the model judged this source to be about a different event"
            selected = select_results(results, cfg)
        if dropped_dates:
            notes.append(f"{dropped_dates} source(s) dropped: published long before the research window")
        notes.extend(f"{p}: {n}" for p, n in ((name, prov.note()) for name, prov in self.providers.items()) if n)
        if extra is None and cfg.model_queries and self.llm is not None and self._model_dead:
            notes.append("model keyword help unavailable; template queries only")
        limitations = package_limitations(selected, results, statuses, cfg, notes)

        coverage = {qt: sum(1 for r in selected if qt in r.query_types) for qt in QUERY_TYPES}
        mix: dict[str, int] = {}
        for r in selected:
            mix[r.content_type] = mix.get(r.content_type, 0) + 1
        status = "completed" if len(selected) >= min(cfg.min_results, cfg.max_results) else "partial"
        pkg = ResearchPackage(
            run_id=run_id, event_id=brief.event_id, event_title=brief.title, status=status,
            window_start=window_start, window_end=now, event_date=brief.first_article_at, queries=statuses,
            result_ids=[r.result_id for r in selected], candidate_count=len(candidates), coverage=coverage,
            content_mix=mix, limitations=limitations,
            stats={"hits": len(hits), "candidates": len(candidates), "assessed": len(results),
                   "pages": {k: sum(1 for p in pages.values() if p.status == k) for k in
                             sorted({p.status for p in pages.values()})},
                   "model": model_stats or {}},
            created_at=datetime.now(timezone.utc))
        self.store.save_event_research(pkg, results, statuses)
        return pkg

    # -- steps --------------------------------------------------------------------------------
    def _search(self, queries: list[ResearchQuery]) -> tuple[list[QueryStatus], list[SearchHit]]:
        statuses, hits = [], []
        for q in queries:
            prov = self.providers.get(q.provider)
            if prov is None:
                continue
            try:
                resp = prov.search(q)
            except Exception as e:  # noqa: BLE001 - a provider bug must not fail the event
                log.exception("%s search failed", q.provider)
                resp = SearchResponse(provider=q.provider, query_id=q.query_id, status="error",
                                      error=f"{type(e).__name__}: {str(e)[:200]}")
            statuses.append(QueryStatus(query_id=q.query_id, query_type=q.query_type, covers=q.covers,
                                        provider=q.provider, text=q.text, status=resp.status, hits=len(resp.hits),
                                        error=resp.error))
            hits.extend(resp.hits)
        return statuses, hits

    def _fetch_pages(self, candidates: list[Candidate]) -> dict[str, PageInfo]:
        """Fetch the most promising pages (by metadata relevance) within the per-event and
        per-run budgets. Pages known to be blocked/paywalled are answered from the cache."""
        pcfg = self.cfg.pages
        out: dict[str, PageInfo] = {}
        if self.pages is None or not pcfg.enabled:
            return out
        floor = self.cfg.min_relevance * 0.5
        ranked = sorted((c for c in candidates if c.prescore >= floor), key=lambda c: (-c.prescore, c.canonical_url))
        todo: list[Candidate] = []
        max_age = timedelta(days=pcfg.cache_days)
        for c in ranked:
            if len(todo) >= pcfg.max_pages_per_event:
                break
            if c.canonical_url in self._page_text:  # fetched earlier in this run for another event
                out[c.canonical_url] = self._page_text[c.canonical_url]
                continue
            cached = self.store.page_cache_get(c.canonical_url, max_age) if pcfg.cache_days else None
            if cached is not None and cached.status in NOT_FETCHED_STATUSES_TO_CACHE:
                out[c.canonical_url] = cached
                continue
            if self.pages_fetched + len(todo) >= pcfg.max_pages_per_run:
                break
            todo.append(c)
        if not todo:
            return out

        def fetch(c: Candidate) -> tuple[Candidate, PageInfo]:
            return c, self.pages.fetch(c.url)

        with ThreadPoolExecutor(max_workers=pcfg.workers, thread_name_prefix="page") as pool:
            for c, page in pool.map(fetch, _spread_hosts(todo)):
                self.pages_fetched += 1
                self.page_stats[page.status] = self.page_stats.get(page.status, 0) + 1
                out[c.canonical_url] = page
                self._page_text[c.canonical_url] = page
                if pcfg.cache_days:
                    self.store.page_cache_put(c.canonical_url, page)
        return out

    def _build_result(self, brief: EventBrief, c: Candidate, page: PageInfo | None, now: datetime,
                      window_start: datetime, background_start: datetime, rel: RelevanceModel, terms: list[str],
                      entities: list[str], retrieved: datetime) -> ResearchResult | None:
        pcfg = self.cfg.pages
        d = resolve_date([(x.value, x.source) for x in c.dates], page, now, window_start, background_start,
                         brief.first_article_at)
        if not d.keep or (not d.in_window and d.published_at is not None and not self.cfg.allow_background):
            return None
        title = c.title or (page.title if page else "") or c.url
        ctype, cevidence = classify_content(c.url, title, c.section, c.source_type, page.schema_type if page else None)
        text = page.text if page is not None and page.status == "fetched" else None
        notes, claims = [], []
        if text:
            notes = extract_evidence(text, terms, entities, max_notes=pcfg.max_extracts_per_result,
                                     max_chars=pcfg.max_extract_chars)
            claims = extract_claims(text, terms, entities, max_claims=3, max_chars=pcfg.max_extract_chars)
            shown = {n.text for n in notes}
            claims = [c for c in claims if c not in shown]  # already quoted as an extract
            notes, claims = _within_budget(notes, claims, len(text))
        elif c.snippet and (page is None or page.status not in ("metadata_only", "paywalled")):
            # A provider's own short summary is metadata; skip it when the page forbids snippets.
            sn = snippet_notes(c.snippet, title, pcfg.max_extract_chars)
            notes = list(sn) if isinstance(sn, (list, tuple)) else ([sn] if sn else [])
        notes_text = " ".join(n.text for n in notes)
        score, why = rel.score(title, f"{c.snippet or ''} {notes_text}")
        qtypes = label_query_types(title, c.snippet, notes_text, ctype, c.source_type, brief.countries, c.query_types)
        canon = c.canonical_url
        publisher = c.publisher or (page.sitename if page and page.sitename else None) or c.domain or domain_of(c.url) or "unknown"
        return ResearchResult(
            result_id=stable_hash(brief.event_id, canon), event_id=brief.event_id, title=title, url=c.url,
            canonical_url=canon, publisher=publisher, domain=c.domain or domain_of(c.url),
            published_at=d.published_at, date_source=d.date_source, date_notes=d.notes,
            event_date=brief.first_article_at, event_timing=d.event_timing, in_window=d.in_window,
            content_type=ctype, content_type_evidence=cevidence, query_types=qtypes, providers=c.providers,
            relevance_score=round(max(score, c.prescore if not text else score), 3), relevance_evidence=why,
            key_claims=claims, evidence_notes=notes, page_status=page.status if page else "not_fetched",
            language=c.language, source_type=c.source_type, from_event=c.from_event,
            syndicated_with=c.syndicated_with, retrieved_at=retrieved)

    def _model_keywords(self, brief: EventBrief, articles: list[Article]) -> dict[str, list[str]] | None:
        if not self.cfg.model_queries or not self._model_ok():
            return None
        heads = [a.title for a in sorted(articles, key=lambda x: x.effective_time, reverse=True)[:8]]
        self.model_calls += 1
        out, err = model_assist.safe_call(model_assist.propose_keywords, self.llm, brief, heads)
        if err:
            self._model_error(brief.event_id, err)
        return out

    def _model_notes(self, brief: EventBrief, selected: list[ResearchResult]) -> dict | None:
        if not self.cfg.model_notes or not selected or not self._model_ok():
            return None
        self.model_calls += 1
        out, err = model_assist.safe_call(model_assist.write_notes, self.llm, brief, selected)
        if err:
            self._model_error(brief.event_id, err)
        return out

    def _model_ok(self) -> bool:
        return not self._model_dead and self.model_calls < self.cfg.max_model_calls

    def _model_error(self, event_id: str, err: dict) -> None:
        self.model_errors.append({"event_id": event_id, **err})
        if err.get("fatal"):
            self._model_dead = True


MAX_STORED_SHARE = 0.25   # never quote more than a quarter of a page...
MAX_STORED_CHARS = 900    # ...or more than this many characters in total


def _within_budget(notes, claims, page_chars: int):
    """Keep quotations short relative to the page, so a short release is never stored whole."""
    budget = min(MAX_STORED_CHARS, int(page_chars * MAX_STORED_SHARE))
    kept_n, kept_c, used = [], [], 0
    for n in notes:
        if used + len(n.text) <= budget:
            kept_n.append(n)
            used += len(n.text)
    for c in claims:
        if used + len(c) <= budget:
            kept_c.append(c)
            used += len(c)
    return kept_n, kept_c


def _spread_hosts(cands: list[Candidate]) -> list[Candidate]:
    """Interleave hosts so parallel workers hit different sites (each host stays throttled)."""
    by_host: dict[str, list[Candidate]] = {}
    for c in cands:
        by_host.setdefault(c.domain or domain_of(c.url) or "", []).append(c)
    out: list[Candidate] = []
    while any(by_host.values()):
        for host in list(by_host):
            if by_host[host]:
                out.append(by_host[host].pop(0))
    return out


def canonical(url: str) -> str:
    try:
        return canonicalize_url(url)
    except ValueError:
        return url
