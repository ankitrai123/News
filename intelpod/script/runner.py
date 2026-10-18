"""The `script` pipeline stage (Phase 5) and `intelpod script`."""
from __future__ import annotations

from ..debate.store import DebateStore
from ..impact.store import ImpactStore
from ..research.runner import learn_vocabulary, model_client
from ..research.store import ResearchStore
from .service import ScriptService
from .store import EpisodeStore


def gather(pipe, run_id: str):
    ranked = pipe.repo.scores_for_run(run_id, selected_only=True)
    debates = DebateStore(pipe.db).packages(run_id)
    istore = ImpactStore(pipe.db)
    impacts = istore.analyses(run_id)
    rstore = ResearchStore(pipe.db)
    sources = {r.event_id: {x.result_id: x for x in rstore.results(run_id, r.event_id)} for r in ranked}
    return ranked, debates, impacts, sources, istore.links(run_id)


def run_script(pipe, ctx, refresh: bool = False) -> dict:
    cfg = pipe.settings.script
    if not cfg.enabled:
        return {"status": "skipped", "reason": "script.enabled is false"}
    store = EpisodeStore(pipe.db)
    if not refresh and (old := store.episode(ctx.run_id)) is not None and old.status in ("validated", "partial"):
        return {"status": "completed", "episode_id": old.episode_id, "reused": True}
    ranked, debates, impacts, sources, links = gather(pipe, ctx.run_id)
    if not ranked:
        return {"status": "skipped", "reason": "no selected events"}
    notes: dict[str, str] = {}
    llm = model_client(pipe, ctx, notes, "script writing") if cfg.model_writing and cfg.max_model_calls > 0 else None
    learn_vocabulary(pipe, ctx)
    service = ScriptService(pipe.settings, llm)
    ep = service.build(ctx.run_id, ctx.now, ranked, debates, impacts, sources, links)
    store.save(ep)
    return {"status": "completed", "episode_id": ep.episode_id, "episode_status": ep.status,
            "stories": len(ep.plan.stories), "words": ep.word_count, "minutes": ep.est_minutes,
            "written_by": ep.written_by, "verification": ep.verification.model_dump(exclude={"problems"}),
            "model_calls": service.model_calls, "model_errors": service.model_errors[:5], "notes": notes}
