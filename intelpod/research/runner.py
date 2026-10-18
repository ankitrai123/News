"""The `research` pipeline stage: builds providers and runs ResearchService for a run's
selected events. Also used by `intelpod research`, and by Phase 3 for viewpoint searches."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import HttpConfig
from ..http import HttpClient
from ..llm.client import LLMClient
from .feeds import active_feeds, load_research_feeds, refresh_research_feeds
from .pages import PageFetcher
from .providers import (CachedSearch, GdeltSearch, GuardianSearch, LocalSearch, article_docs,
                        research_item_docs)
from .service import ResearchService, make_brief
from .store import ResearchStore

log = logging.getLogger(__name__)

API_UA = "IntelPod/0.1 (personal research)"


@dataclass
class Toolkit:
    """HTTP clients, providers, page fetcher and model client for one stage."""

    providers: dict
    pages: PageFetcher
    llm: object | None
    notes: dict[str, str] = field(default_factory=dict)
    feed_stats: dict = field(default_factory=dict)
    clients: list[HttpClient] = field(default_factory=list)

    def close(self, repo, run_id: str) -> None:
        for p in self.providers.values():
            inner = getattr(p, "inner", p)
            if getattr(inner, "fetches", None):
                repo.log_fetches(run_id, inner.fetches)
                inner.fetches = []
        for c in self.clients:
            c.close()


def selected_events(pipe, ctx, limit: int):
    """(brief, articles) for the run's selected events, best first."""
    repo = pipe.repo
    selected = sorted(repo.scores_for_run(ctx.run_id, selected_only=True), key=lambda r: (r.rank or 99, -r.importance))
    cand_list = (ctx.outputs.get("review") or {}).get("candidates") or (ctx.outputs.get("cluster") or {}).get("candidates") or []
    cands = {c["event_id"]: c for c in cand_list}
    ids = sorted({a for r in selected for a in (cands.get(r.event_id) or {}).get("article_ids", [])})
    articles = repo.get_articles(ids)
    events = []
    for r in selected[:limit]:
        arts = [articles[a] for a in (cands.get(r.event_id) or {}).get("article_ids", []) if a in articles]
        events.append((make_brief(ctx.run_id, r, cands.get(r.event_id), arts), arts))
    return events


def build_toolkit(pipe, ctx, store: ResearchStore, refresh_feeds: bool, gdelt_max_requests: int,
                  want_model: bool, model_reason: str = "model help") -> Toolkit:
    settings = pipe.settings
    cfg = settings.research
    repo = pipe.repo
    web = HttpClient(settings.http, settings.app.user_agent)            # pages and feeds: identify ourselves
    gdelt_http = HttpClient(HttpConfig(**{**settings.http.model_dump(), "timeout_seconds": 60}), API_UA)
    api_http = HttpClient(settings.http, API_UA)
    notes: dict[str, str] = {}
    feed_stats: dict = {}
    providers: dict = {}
    if cfg.archive.enabled:
        providers["archive"] = LocalSearch("archive", lambda s, e: article_docs(repo.articles_in_window(s, e)),
                                           pipe.taxonomy)
    if cfg.feeds.enabled:
        if refresh_feeds:
            feeds = load_research_feeds(settings.resolve(cfg.feeds.feeds_file))
            active, paused = active_feeds(feeds, repo.feed_states(), settings.ingestion.feed_pause_after_failures,
                                          settings.ingestion.feed_retry_paused_after_hours)
            if paused:
                notes["feeds"] = f"{len(paused)} research feed(s) paused after failures or robots.txt refusal"
            feed_stats = refresh_research_feeds(web, active, repo, store, ctx.run_id,
                                                settings.ingestion.max_description_chars,
                                                workers=settings.ingestion.providers.rss.max_workers)
        providers["feeds"] = LocalSearch("feeds", lambda s, e: research_item_docs(store.items_in_window(s, e)),
                                         pipe.taxonomy)
    cache_age = timedelta(hours=cfg.search_cache_hours)
    ingest_gdelt = ((ctx.outputs.get("ingest") or {}).get("providers") or {}).get("gdelt") or {}
    research_gdelt = " ".join((ctx.outputs.get("research") or {}).get("provider_notes", {}).values())
    if cfg.gdelt.enabled and gdelt_max_requests > 0:
        if "refusing" in (ingest_gdelt.get("note") or "") or "refusing" in research_gdelt:
            notes["gdelt"] = "GDELT refused requests earlier in this run; not queried again"
        else:
            from ..providers.rss import load_feeds
            domain_pub = {f.domain: f.publisher for f in load_feeds(settings.resolve(settings.ingestion.feeds_file),
                                                                    include_disabled=True) if f.domain}
            gcfg = cfg.gdelt.model_copy(update={"max_requests_per_run": gdelt_max_requests})
            providers["gdelt"] = CachedSearch(GdeltSearch(gdelt_http, gcfg, settings.ingestion.providers.gdelt,
                                                          domain_pub), store, cache_age)
    if cfg.guardian.enabled and cfg.guardian.max_requests_per_run > 0:
        from ..logging_setup import read_secret
        key = read_secret(cfg.guardian.api_key_env) if cfg.guardian.api_key_env else None
        if key:
            providers["guardian"] = CachedSearch(GuardianSearch(api_http, key, cfg.guardian.max_requests_per_run,
                                                                cfg.guardian.page_size), store, cache_age)
        else:
            notes["guardian"] = f"{cfg.guardian.api_key_env} not set (optional)"
    llm = None
    review = ctx.outputs.get("review") or {}
    if not want_model:
        pass
    elif review.get("status") == "failed_all" and any(e.get("fatal") for e in review.get("errors") or []):
        notes["model"] = f"{model_reason} not used: the model provider failed during review ({review.get('reason', '')[:120]})"
    elif settings.llm.enabled:
        client = pipe._make_llm(ctx.run_id)
        ok, why = client.availability() if isinstance(client, LLMClient) else (True, "ok")
        if ok:
            llm = client
        else:
            notes["model"] = f"{model_reason} not used: {why}"
    else:
        notes["model"] = f"{model_reason} not used: llm.enabled is false"
    pages = PageFetcher(web, cfg.pages, no_extract_domains=tuple(cfg.pages.no_extract_domains))
    return Toolkit(providers=providers, pages=pages, llm=llm, notes=notes, feed_stats=feed_stats,
                   clients=[web, gdelt_http, api_http])


def load_registry(settings):
    from ..debate.registry import Registry
    return Registry.load(settings.resolve(settings.debate.sources_file))


def run_research(pipe, ctx, refresh: bool = False) -> dict:
    settings = pipe.settings
    cfg = settings.research
    if not cfg.enabled:
        return {"status": "skipped", "reason": "research.enabled is false"}
    store = ResearchStore(pipe.db)
    events = selected_events(pipe, ctx, cfg.max_events)
    if not events:
        return {"status": "skipped", "reason": "no selected events to research"}
    tk = build_toolkit(pipe, ctx, store, refresh_feeds=True, gdelt_max_requests=cfg.gdelt.max_requests_per_run,
                       want_model=(cfg.model_queries or cfg.model_notes) and cfg.max_model_calls > 0)
    try:
        service = ResearchService(settings, store, pipe.taxonomy, tk.providers, page_fetcher=tk.pages,
                                  llm_client=tk.llm, registry=load_registry(settings))
        stats = service.research_events(ctx.run_id, events, ctx.now, skip_done=not refresh)
        store.prune_items(datetime.now(timezone.utc) - timedelta(days=cfg.feeds.keep_days))
        store.prune_caches(datetime.now(timezone.utc) - timedelta(days=2),
                           datetime.now(timezone.utc) - timedelta(days=max(1, cfg.pages.cache_days)))
    finally:
        tk.close(pipe.repo, ctx.run_id)
    pkgs = store.packages(ctx.run_id)
    return {
        "status": "completed", "events": len(events), "providers": sorted(tk.providers),
        "provider_notes": {**tk.notes, **stats.get("provider_notes", {})}, "research_feeds": tk.feed_stats,
        "packages": {eid: {"status": p.status, "selected": len(p.result_ids), "candidates": p.candidate_count,
                           "limitations": len(p.limitations)} for eid, p in pkgs.items()},
        **{k: v for k, v in stats.items() if k != "provider_notes"},
        "attribution": "Discovery via The GDELT Project (https://www.gdeltproject.org/)" if "gdelt" in tk.providers else None,
    }
