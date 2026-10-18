"""Local dashboard + JSON API. Binds to 127.0.0.1 by default; no secrets are rendered."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__
from ..config import Settings, get_settings
from ..db import Database
from ..logging_setup import read_secret
from ..pipeline.runner import RESUMABLE, RUN_ID_RE, Phase1Pipeline, RunInProgress, StageError, feed_pause_state
from ..providers import load_feeds
from ..repository import Repository
from .. import labels

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["dt"] = labels.dt_utc
TEMPLATES.env.filters["topic"] = labels.topic_label
TEMPLATES.env.filters["flag"] = labels.flag_label
TEMPLATES.env.filters["flag_help"] = labels.flag_help
TEMPLATES.env.filters["date_source"] = labels.date_source_label
TEMPLATES.env.globals["plural"] = labels.plural
TEMPLATES.env.globals["display_entities"] = labels.display_entities

LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]"]
MESSAGES = {
    "started": "Run started. This page refreshes while it runs.",
    "resumed": "Resuming the latest unfinished run.",
    "busy": "A run is already in progress; wait for it to finish.",
    "nothing": "Nothing to resume: the latest run already completed. Use Start new run.",
    "norun": "That run does not exist.",
    "notresumable": "That run cannot be resumed (it completed or is still running).",
}
PLACEHOLDER_CONTACT = "set-your-email@example.com"


def create_app(settings: Settings | None = None, pipeline_factory=None,
               allowed_hosts: list[str] | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_file)
    repo = Repository(db)
    app = FastAPI(title="IntelPod", version=__version__,
                  description="Local API for the geopolitical & business intelligence podcast agent (Phase 1).")
    hosts = list(allowed_hosts or []) + LOCAL_HOSTS
    # Block DNS-rebinding: only answer requests addressed to this machine's local names.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        """Runs cost NewsData credits and NVIDIA requests: refuse state-changing requests
        sent by other websites (cross-site form posts / fetches)."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin") or request.headers.get("referer")
            if site and site not in ("same-origin", "none"):
                return PlainTextResponse("cross-site request refused", status_code=403)
            if origin:
                host = urlsplit(origin).hostname or ""
                if host not in hosts:
                    return PlainTextResponse("cross-origin request refused", status_code=403)
        return await call_next(request)

    state = {"thread": None, "last_error": None}
    make_pipeline = pipeline_factory or (lambda: Phase1Pipeline(settings, db=db))

    def thread_alive() -> bool:
        return state["thread"] is not None and state["thread"].is_alive()

    def active_run():
        """The run in progress, whether started here or from the CLI (DB status 'running')."""
        return repo.active_run()

    def running() -> bool:
        return thread_alive() or active_run() is not None

    def start_run(resume: bool = False, run_id: str | None = None) -> str:
        """Start (or resume) a run in a background thread. Returns a message key."""
        if running():
            raise HTTPException(409, MESSAGES["busy"])
        if run_id is not None and not RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(422, "invalid run id")
        if not resume and run_id is not None:
            raise HTTPException(422, "run_id is only accepted together with resume=true")
        if resume:
            row = repo.get_run(run_id) if run_id else repo.latest_run(status=None)
            if row is None and run_id:
                raise HTTPException(404, MESSAGES["norun"])
            if row is None or row.status not in RESUMABLE:
                raise HTTPException(409, MESSAGES["notresumable"] if run_id else MESSAGES["nothing"])
            run_id = row.run_id

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
        return "resumed" if resume else "started"

    def run_or_404(run_id: str):
        row = repo.get_run(run_id)
        if row is None:
            raise HTTPException(404, f"run '{run_id}' not found")
        return row

    # ---------------------------------------------------------------- JSON API
    @app.get("/api/health")
    def health():
        act = active_run()
        return {"status": "ok", "version": __version__, "schema_version": db.schema_version(),
                "articles": repo.count_articles(), "run_in_progress": running(),
                "active_run": {"run_id": act.run_id, "stage": repo.current_stage(act.run_id)} if act else None,
                "last_error": state["last_error"]}

    @app.get("/api/runs")
    def api_runs(limit: int = Query(30, ge=1, le=500)):
        return [{"run_id": r.run_id, "kind": r.kind, "status": r.status, "started_at": r.started_at,
                 "finished_at": r.finished_at, "error": r.error} for r in repo.list_runs(limit)]

    @app.post("/api/runs", status_code=202)
    def api_start_run(resume: bool = False, run_id: str | None = Query(None, max_length=80)):
        key = start_run(resume=resume, run_id=run_id)
        return {"accepted": True, "resume": resume, "message": MESSAGES[key]}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        run = run_or_404(run_id)
        stages = {k: {"status": v.status, "attempts": v.attempts, "error": v.error, "started_at": v.started_at,
                      "finished_at": v.finished_at,
                      "summary": {kk: vv for kk, vv in (v.output or {}).items() if kk != "candidates"}}
                  for k, v in repo.stages(run_id).items()}
        return {"run_id": run.run_id, "status": run.status, "params": run.params, "error": run.error, "stages": stages}

    @app.get("/api/runs/{run_id}/events")
    def api_run_events(run_id: str, selected_only: bool = True):
        run_or_404(run_id)
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
                        "auto_paused": feed_pause_state(st, settings.ingestion) is not None,
                        "pause_reason": feed_pause_state(st, settings.ingestion),
                        "last_error": st.last_error if st else None})
        return out

    @app.get("/api/usage")
    def api_usage():
        return {"providers_24h": repo.provider_usage(), "llm_24h": repo.llm_usage(),
                "newsdata_requests_today_utc": repo.requests_today("newsdata"),
                "newsdata_daily_budget": settings.ingestion.providers.newsdata.daily_credit_budget}

    # ------------------------------------------------------------------- pages
    def page(request: Request, name: str, active: str = "", status_code: int = 200, **ctx):
        act = active_run()
        ctx.update(request=request, running=running(), active_run=act,
                   active_stage=repo.current_stage(act.run_id) if act else None,
                   last_error=state["last_error"], version=__version__, active=active,
                   message=MESSAGES.get(request.query_params.get("msg", ""), ""))
        return TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)

    @app.exception_handler(StarletteHTTPException)
    async def html_errors(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or exc.status_code not in (404, 409):
            return await http_exception_handler(request, exc)
        return page(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, run: str | None = Query(None, max_length=80)):
        # Latest completed run; if there is none yet, the latest run in any state (in progress / failed).
        run_row = run_or_404(run) if run else (repo.latest_run() or repo.latest_run(status=None))
        ranked = repo.scores_for_run(run_row.run_id) if run_row else []
        stages = repo.stages(run_row.run_id) if run_row else {}
        out = lambda k: (stages.get(k).output or {}) if stages.get(k) else {}  # noqa: E731
        return page(request, "index.html", active="briefing", run=run_row, selected=[r for r in ranked if r.selected],
                    others=[r for r in ranked if not r.selected][:15], scored=bool(ranked),
                    run_stage=repo.current_stage(run_row.run_id) if run_row else None,
                    ingest=out("ingest"), review=out("review"), cluster=out("cluster"),
                    min_events=settings.ranking.min_events)

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event_page(request: Request, event_id: str, run: str | None = Query(None, max_length=80)):
        ev = repo.event(event_id)
        if not ev:
            raise HTTPException(404, f"event '{event_id}' not found")
        run_row = run_or_404(run) if run else repo.latest_run()
        score = next((r for r in repo.scores_for_run(run_row.run_id) if r.event_id == event_id), None) if run_row else None
        all_articles = repo.event_articles(event_id)
        run_ids = repo.run_event_article_ids(run_row.run_id, event_id) if run_row and score else None
        if run_ids is not None:
            keep = set(run_ids)
            articles = [a for a in all_articles if a.article_id in keep]
        else:
            articles = all_articles
        return page(request, "event.html", ev=ev, score=score, run=run_row, articles=articles,
                    articles_scope="run" if run_ids is not None else "all", total_linked=len(all_articles),
                    history=repo.event_score_history(event_id))

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        runs = repo.list_runs(30)
        latest = runs[0] if runs else None
        return page(request, "runs.html", active="runs", runs=runs,
                    stages={r.run_id: repo.stages(r.run_id) for r in runs[:10]},
                    can_resume=bool(latest and latest.status in RESUMABLE),
                    failures=[f for f in repo.fetch_log(limit=300) if not f.ok][:50], usage=repo.provider_usage(),
                    llm_usage=repo.llm_usage(), newsdata_today=repo.requests_today("newsdata"),
                    newsdata_budget=settings.ingestion.providers.newsdata.daily_credit_budget,
                    llm_errors=[c for c in repo.llm_calls(limit=200) if not c.ok][:20])

    @app.post("/runs/start")
    def runs_start(resume: str | None = Form(None), run_id: str | None = Form(None, max_length=80)):
        try:
            key = start_run(resume=bool(resume), run_id=run_id or None)
        except HTTPException as e:
            key = next((k for k, v in MESSAGES.items() if v == e.detail), "nothing")
        return RedirectResponse(f"/runs?msg={quote(key)}", status_code=303)

    @app.get("/feeds", response_class=HTMLResponse)
    def feeds_page(request: Request):
        return page(request, "feeds.html", active="feeds", feeds=api_feeds(),
                    pause_after=settings.ingestion.feed_pause_after_failures)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        cfg = settings.model_dump()
        nd = settings.ingestion.providers.newsdata
        keys = {nd.api_key_env: bool(read_secret(nd.api_key_env))} if nd.api_key_env else {}
        for prov in settings.llm.providers.values():
            if prov.api_key_env:
                keys[prov.api_key_env] = bool(read_secret(prov.api_key_env))
        return page(request, "settings.html", active="settings", cfg=cfg, now=datetime.now(timezone.utc), keys=keys,
                    placeholder_contact=PLACEHOLDER_CONTACT in settings.app.user_agent)

    return app
