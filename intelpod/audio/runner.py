"""The `audio` pipeline stage (Phase 6) and `intelpod audio`."""
from __future__ import annotations

import json

from ..script.store import EpisodeStore
from .engines import ENGINES, choose
from .service import render
from .store import AudioStore


def run_audio(pipe, ctx, engine_name: str | None = None, redo: set[str] | None = None) -> dict:
    settings = pipe.settings
    cfg = settings.audio
    if not cfg.enabled:
        return {"status": "skipped", "reason": "audio.enabled is false"}
    ep = EpisodeStore(pipe.db).episode(ctx.run_id)
    if ep is None or not ep.lines:
        return {"status": "skipped", "reason": "no verified script for this run"}
    models_dir = settings.resolve(cfg.models_dir)
    if engine_name:
        eng = ENGINES[engine_name](cfg, models_dir)
        ok, why = eng.available()
        notes = {} if ok else {engine_name: why}
        eng = eng if ok else None
    else:
        eng, notes = choose(cfg, models_dir)
    if eng is None:
        return {"status": "skipped", "reason": "no text-to-speech engine available", "engines": notes}
    run_dir = settings.data_path / "runs" / ctx.run_id
    ar = render(ep, eng, cfg, run_dir, redo=redo, wpm=settings.script.words_per_minute)
    AudioStore(pipe.db).save(ar)
    write_metadata(run_dir, ep, ar)
    return {"status": "completed", "audio_status": ar.status, "engine": ar.engine, "voices": ar.voices,
            "duration_s": ar.duration_s, "files": ar.files, "lines": len(ar.lines),
            "rendered": sum(not ln.cached for ln in ar.lines), "cached": sum(ln.cached for ln in ar.lines),
            "failed": [ln.line_id for ln in ar.lines if ln.status != "ok"],
            "checks_failed": [c.name for c in ar.checks if not c.ok], "engines_skipped": notes}


def write_metadata(run_dir, ep, ar) -> None:
    """episode.json: everything a podcast player or archive needs, next to the audio."""
    eng = ENGINES.get(ar.engine)
    meta = {
        "episode_id": ep.episode_id, "title": ep.title, "run_id": ep.run_id, "status": ar.status,
        "duration_s": ar.duration_s, "files": ar.files, "hosts": ep.hosts,
        "voices": ar.voices, "engine": ar.engine, "engine_licence": eng.licence if eng else "",
        "script_written_by": ep.written_by, "stories": [s.title for s in ep.plan.stories],
        "chapters": [c.model_dump() for c in ar.chapters],
        "transcript": "transcript.json", "sources": "script.md#sources", "evidence_report": "evidence_report.md",
        "episode_notes": "episode_notes.md",
        "lines": [{"id": ln.line_id, "speaker": ep.hosts[ln.speaker], "start_s": ln.start_s,
                   "duration_s": ln.duration_s} for ln in ar.lines],
        "checks": [c.model_dump() for c in ar.checks], "limitations": ar.limitations,
    }
    (run_dir / "audio").mkdir(parents=True, exist_ok=True)
    (run_dir / "episode.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
