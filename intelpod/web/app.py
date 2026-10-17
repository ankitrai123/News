"""Local dashboard + JSON API. Binds to 127.0.0.1 by default; no secrets are rendered."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Settings, get_settings
from ..db import Database
from ..pipeline.runner import Phase1Pipeline, RunInProgress, StageError
from ..providers import load_feeds
from ..repository import Repository

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["dt"] = lambda d: d.strftime("%Y-%m-%d %H:%M") if d else "—"


def create_app(settings: Settings | None = None, pipeline_factory=None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_file)
    repo = Repository(db)
    app = FastAPI(title="IntelPod", version=__version__,
                  description="Local API for the geopolitical & business intelligence podcast agent (Phase 1).")
    state = {"thread": None, "last_error": None}
    make_pipeline = pipeline_factory or (lambda: Phase1Pipeline(settings, db=db))

    def start_run(resume: bool = False, run_id: str | None = None) -> None:
        t = state["thread"]
        if t is not None and t.is_alive():
            raise HTTPException(409, "a run is already in progress")

        def target():
            state["last_error"] = None
            try:
                make_pipeline().run(resume=resume, run_id=run_id)
            except (StageError, RunInProgress, ValueError) as e:
                state["last_error"] = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("background run crashed")
                state["last_error"] = f"{type(e).__name__}: {e}"

        th = threading.Thread(target=target, name="intelpod-run", daemon=True)
        state["thread"] = th
        th.start()

    def running() -> bool:
        return state["thread"] is not None and state["thread"].is_alive()

    # ---------------------------------------------------------------- JSON API
    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "schema_version": db.schema_version(),
                "articles": repo.count_articles(), "run_in_progress": running(), "last_error": state["last_error"]}

    @app.get("/api/runs")
    def api_runs(limit: int = 30):
        return [{"run_id": r.run_id, "kind": r.kind, "status": r.status, "started_at": r.started_at,
                 "finished_at": r.finished_at, "error": r.error} for r in repo.list_runs(limit)]

    @app.post("/api/runs", status_code=202)
    def api_start_run(resume: bool = False, run_id: str | None = None):
        start_run(resume=resume, run_id=run_id)
        return {"accepted": True, "resume": resume}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        run = repo.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        stages = {k: {"status": v.status, "attempts": v.attempts, "error": v.error, "started_at": v.started_at,
                      "finished_at": v.finished_at,
                      "summary": {kk: vv for kk, vv in (v.output or {}).items() if kk != "candidates"}}
                  for k, v in repo.stages(run_id).items()}
        return {"run_id": run.run_id, "status": run.status, "params": run.params, "error": run.error, "stages": stages}

    @app.get("/api/runs/{run_id}/events")
    def api_run_events(run_id: str, selected_only: bool = True):
        return [r.model_dump(mode="json") for r in repo.scores_for_run(run_id, selected_only)]

    @app.get("/api/events/latest")
    def api_latest_events(selected_only: bool = True):
        run = repo.latest_run()
        if not run:
            return {"run_id": None, "events": []}
        return {"run_id": run.run_id, "events": [r.model_dump(mode="json") for r in repo.scores_for_run(run.run_id, selected_only)]}

    @app.get("/api/events/{event_id}")
    def api_event(event_id: str):
        ev = repo.event(event_id)
        if not ev:
            raise HTTPException(404, "event not found")
        return {"event_id": ev.event_id, "title": ev.title, "description": ev.description, "topic": ev.topic,
                "subtopic": ev.subtopic, "countries": ev.countries, "entities": ev.entities,
                "first_seen_at": ev.first_seen_at, "first_article_at": ev.first_article_at,
                "last_development_at": ev.last_development_at,
                "articles": [a.model_dump(mode="json") for a in repo.event_articles(event_id)],
                "score_history": repo.event_score_history(event_id)}

    @app.get("/api/feeds")
    def api_feeds():
        states = repo.feed_states()
        out = []
        for f in load_feeds(settings.resolve(settings.ingestion.feeds_file), include_disabled=True):
            st = states.get(f.url)
            out.append({"name": f.name, "publisher": f.publisher, "url": f.url, "enabled": f.enabled,
                        "verified": f.verified, "source_type": f.source_type, "country": f.country,
                        "last_status": st.last_status if st else None, "last_ok_at": st.last_ok_at if st else None,
                        "last_item_count": st.last_item_count if st else None,
                        "newest_item_at": st.newest_item_at if st else None,
                        "consecutive_failures": st.consecutive_failures if st else None,
                        "last_error": st.last_error if st else None})
        return out

    @app.get("/api/usage")
    def api_usage():
        return {"providers_24h": repo.provider_usage(), "llm_24h": repo.llm_usage(),
                "newsdata_requests_today_utc": repo.requests_today("newsdata"),
                "newsdata_daily_budget": settings.ingestion.providers.newsdata.daily_credit_budget}

    # ------------------------------------------------------------------- pages
    def page(request: Request, name: str, **ctx):
        ctx.update(request=request, running=running(), last_error=state["last_error"], version=__version__)
        return TEMPLATES.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, run: str | None = None):
        run_row = repo.get_run(run) if run else repo.latest_run()
        ranked = repo.scores_for_run(run_row.run_id) if run_row else []
        stages = repo.stages(run_row.run_id) if run_row else {}
        return page(request, "index.html", run=run_row, selected=[r for r in ranked if r.selected],
                    others=[r for r in ranked if not r.selected][:15],
                    ingest=(stages.get("ingest").output or {}) if stages.get("ingest") else {},
                    review=(stages.get("review").output or {}) if stages.get("review") else {},
                    cluster=(stages.get("cluster").output or {}) if stages.get("cluster") else {})

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event_page(request: Request, event_id: str, run: str | None = None):
        ev = repo.event(event_id)
        if not ev:
            raise HTTPException(404, "event not found")
        run_row = repo.get_run(run) if run else repo.latest_run()
        score = next((r for r in repo.scores_for_run(run_row.run_id) if r.event_id == event_id), None) if run_row else None
        return page(request, "event.html", ev=ev, score=score, articles=repo.event_articles(event_id),
                    history=repo.event_score_history(event_id))

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        runs = repo.list_runs(30)
        return page(request, "runs.html", runs=runs, stages={r.run_id: repo.stages(r.run_id) for r in runs[:10]},
                    failures=[f for f in repo.fetch_log(limit=300) if not f.ok][:50], usage=repo.provider_usage(),
                    llm_usage=repo.llm_usage(), newsdata_today=repo.requests_today("newsdata"),
                    newsdata_budget=settings.ingestion.providers.newsdata.daily_credit_budget,
                    llm_errors=[c for c in repo.llm_calls(limit=200) if not c.ok][:20])

    @app.post("/runs/start")
    def runs_start(resume: str | None = None):
        try:
            start_run(resume=bool(resume))
        except HTTPException:
            pass
        return RedirectResponse("/runs", status_code=303)

    @app.get("/feeds", response_class=HTMLResponse)
    def feeds_page(request: Request):
        return page(request, "feeds.html", feeds=api_feeds())

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        cfg = settings.model_dump()
        return page(request, "settings.html", cfg=cfg, now=datetime.now(timezone.utc))

    return app
