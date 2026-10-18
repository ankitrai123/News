"""The `impact` pipeline stage (Phase 4) and `intelpod impact`."""
from __future__ import annotations

from ..debate.store import DebateStore
from ..research.runner import model_client, selected_events
from ..research.store import ResearchStore
from .service import ImpactService
from .store import ImpactStore


def run_impact(pipe, ctx, refresh: bool = False) -> dict:
    settings = pipe.settings
    cfg = settings.impact
    if not cfg.enabled:
        return {"status": "skipped", "reason": "impact.enabled is false"}
    events = [b for b, _ in selected_events(pipe, ctx, settings.research.max_events)]
    if not events:
        return {"status": "skipped", "reason": "no selected events"}
    notes: dict[str, str] = {}
    want = cfg.model_analysis and cfg.max_model_calls > 0
    llm = model_client(pipe, ctx, notes, "impact analysis") if want else None
    service = ImpactService(settings, ResearchStore(pipe.db), DebateStore(pipe.db), ImpactStore(pipe.db),
                            llm_client=llm)
    stats = service.run(ctx.run_id, events, ctx.now, skip_done=not refresh)
    return {"status": "completed", "notes": notes, **stats}
