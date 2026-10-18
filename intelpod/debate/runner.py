"""The `debate` pipeline stage (Phase 3): claim map, diversity, viewpoint searches and checked
model analysis for every researched event. Also used by `intelpod debate`."""
from __future__ import annotations

from ..research.runner import build_toolkit, learn_vocabulary, load_registry, selected_events
from ..research.service import ResearchService
from ..research.store import ResearchStore
from .service import DebateService
from .store import DebateStore


def run_debate(pipe, ctx, refresh: bool = False, use_model: bool = True) -> dict:
    settings = pipe.settings
    cfg = settings.debate
    if not cfg.enabled:
        return {"status": "skipped", "reason": "debate.enabled is false"}
    rstore = ResearchStore(pipe.db)
    researched = rstore.packages(ctx.run_id)
    events = [(b, a) for b, a in selected_events(pipe, ctx, settings.research.max_events) if b.event_id in researched]
    if not events:
        return {"status": "skipped", "reason": "no researched events (the research stage produced no packages)"}
    registry = load_registry(settings)
    learn_vocabulary(pipe, ctx)
    want_model = use_model and cfg.model_analysis and cfg.max_model_calls > 0
    searching = cfg.viewpoint_searches_per_event > 0 and settings.research.enabled
    tk = build_toolkit(pipe, ctx, rstore, refresh_feeds=False,
                       gdelt_max_requests=cfg.gdelt_requests_per_run if searching else 0,
                       want_model=want_model, model_reason="debate analysis")
    try:
        research = ResearchService(settings, rstore, pipe.taxonomy, tk.providers, page_fetcher=tk.pages,
                                   registry=registry) if searching and tk.providers else None
        service = DebateService(settings, rstore, DebateStore(pipe.db), registry, pipe.taxonomy,
                                research_service=research, llm_client=tk.llm if want_model else None)
        stats = service.debate_events(ctx.run_id, events, ctx.now, skip_done=not refresh)
    finally:
        tk.close(pipe.repo, ctx.run_id)
    pkgs = DebateStore(pipe.db).packages(ctx.run_id)
    notes = dict(tk.notes)
    if research is not None:
        notes.update({n: x for n, p in tk.providers.items() if (x := p.note())})
    return {
        "status": "completed", "events": len(events), "registry_sources": len(registry.profiles),
        "provider_notes": notes,
        "packages": {eid: {"status": p.status, "claims": len(p.claims), "questions": len(p.questions),
                           "contested": len(p.contested), "confidence": p.overall_confidence,
                           "sources": len(p.source_ids)} for eid, p in pkgs.items()},
        **{k: v for k, v in stats.items() if k != "events"}, "per_event": stats["events"],
    }
