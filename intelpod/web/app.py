"""Local dashboard + JSON API. Binds to 127.0.0.1 by default; no secrets are rendered."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
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
from ..research.models import QUERY_TYPE_LABELS
from ..research.store import ResearchStore
from ..debate.models import CONFIDENCE_HELP
from ..debate.store import DebateStore
from ..impact.models import DIMENSIONS, SECTORS
from ..impact.store import ImpactStore
from ..script.store import EpisodeStore
from ..audio.store import AudioStore

log = logging.getLogger(__name__)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["dt"] = labels.dt_utc
TEMPLATES.env.filters["topic"] = labels.topic_label
TEMPLATES.env.filters["flag"] = labels.flag_label
TEMPLATES.env.filters["flag_help"] = labels.flag_help
TEMPLATES.env.filters["date_source"] = labels.date_source_label
TEMPLATES.env.globals["plural"] = labels.plural
TEMPLATES.env.globals["display_entities"] = labels.display_entities
TEMPLATES.env.globals["qlabels"] = QUERY_TYPE_LABELS
TEMPLATES.env.filters["safe_url"] = lambda u: u if isinstance(u, str) and u.startswith(("http://", "https://")) else "#"

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
    rstore = ResearchStore(db)
    dstore = DebateStore(db)
    istore = ImpactStore(db)
    estore = EpisodeStore(db)
    astore = AudioStore(db)
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

    @app.get("/api/runs/{run_id}/research")
    def api_run_research(run_id: str):
        run_or_404(run_id)
        return {eid: {"status": p.status, "sources": len(p.result_ids), "candidates": p.candidate_count,
                      "coverage": p.coverage, "content_mix": p.content_mix, "limitations": p.limitations}
                for eid, p in rstore.packages(run_id).items()}

    @app.get("/api/events/{event_id}/research")
    def api_event_research(event_id: str, run: str | None = Query(None, max_length=80), all_sources: bool = False):
        if repo.event(event_id) is None:
            raise HTTPException(404, "event not found")
        pkg = rstore.package(run, event_id) if run else rstore.latest_package(event_id)
        if pkg is None:
            raise HTTPException(404, "no research for this event" + (f" in run {run}" if run else ""))
        results = rstore.results(pkg.run_id, event_id, selected_only=not all_sources)
        return {"package": pkg.model_dump(mode="json"), "sources": [r.model_dump(mode="json") for r in results]}

    @app.get("/api/runs/{run_id}/debate")
    def api_run_debate(run_id: str):
        run_or_404(run_id)
        return {eid: {"status": p.status, "claims": len(p.claims), "consensus": len(p.consensus),
                      "contested": len(p.contested), "unverified": len(p.unverified), "questions": len(p.questions),
                      "overall_confidence": p.overall_confidence, "no_disagreement_note": p.no_disagreement_note,
                      "limitations": p.limitations}
                for eid, p in dstore.packages(run_id).items()}

    @app.get("/api/events/{event_id}/debate")
    def api_event_debate(event_id: str, run: str | None = Query(None, max_length=80)):
        if repo.event(event_id) is None:
            raise HTTPException(404, "event not found")
        pkg = dstore.package(run, event_id) if run else dstore.latest_package(event_id)
        if pkg is None:
            raise HTTPException(404, "no debate package for this event" + (f" in run {run}" if run else ""))
        return pkg.model_dump(mode="json")

    @app.get("/api/runs/{run_id}/brief")
    def api_run_brief(run_id: str):
        run_or_404(run_id)
        b = istore.brief(run_id)
        if b is None:
            raise HTTPException(404, "no intelligence brief for this run")
        return {"brief": b.model_dump(mode="json"),
                "events": {eid: {"status": a.status, "what_happened": a.context.what_happened,
                                 "dimensions": [d.key for d in a.dimensions], "chains": len(a.chains),
                                 "india": a.india is not None, "limitations": a.limitations}
                           for eid, a in istore.analyses(run_id).items()}}

    @app.get("/api/events/{event_id}/impact")
    def api_event_impact(event_id: str, run: str | None = Query(None, max_length=80)):
        if repo.event(event_id) is None:
            raise HTTPException(404, "event not found")
        a = istore.analysis(run, event_id) if run else istore.latest_analysis(event_id)
        if a is None:
            raise HTTPException(404, "no impact analysis for this event" + (f" in run {run}" if run else ""))
        return a.model_dump(mode="json")

    @app.get("/api/runs/{run_id}/episode")
    def api_run_episode(run_id: str):
        run_or_404(run_id)
        ep = estore.episode(run_id)
        if ep is None:
            raise HTTPException(404, "no episode for this run")
        return ep.model_dump(mode="json")

    @app.get("/api/forecasts")
    def api_forecasts(status: str | None = Query(None, pattern="^(open|resolved)$"),
                      event_id: str | None = Query(None, max_length=40)):
        return [f.model_dump(mode="json") for f in istore.forecasts(status=status, event_id=event_id)]

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
                    research=rstore.packages(run_row.run_id) if run_row else {},
                    debates=dstore.packages(run_row.run_id) if run_row else {},
                    impacts=istore.analyses(run_row.run_id) if run_row else {},
                    brief=istore.brief(run_row.run_id) if run_row else None,
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
        pkg = (rstore.package(run_row.run_id, event_id) if run_row else None) or rstore.latest_package(event_id)
        results = rstore.results(pkg.run_id, event_id) if pkg else []
        order = {rid: i for i, rid in enumerate(pkg.result_ids)} if pkg else {}
        sources = sorted((r for r in results if r.selected), key=lambda r: order.get(r.result_id, 99))
        debate = dstore.package(pkg.run_id, event_id) if pkg else None
        impact = (istore.analysis(run_row.run_id, event_id) if run_row else None) or istore.latest_analysis(event_id)
        by_id = {r.result_id: r for r in results}
        claims = {c.claim_id: c for c in debate.claims} if debate else {}
        return page(request, "event.html", ev=ev, score=score, run=run_row, articles=articles,
                    research=pkg, sources=sources, rejected=[r for r in results if not r.selected][:40],
                    queries=pkg.queries if pkg else [], debate=debate, src=by_id, claims=claims,
                    confidence_help=CONFIDENCE_HELP, impact=impact, sector_labels=SECTORS, dim_labels=DIMENSIONS,
                    event_forecasts=istore.forecasts(event_id=event_id, limit=20),
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

    @app.get("/episode", response_class=HTMLResponse)
    def episode_page(request: Request, run: str | None = Query(None, max_length=80)):
        if run:
            run_or_404(run)
            ep = estore.episode(run)
        else:
            eps = estore.episodes(1)
            ep = eps[0] if eps else None
        cites, listed = {}, []
        if ep is not None:
            from ..script.report import citations
            srcs = {x.result_id: x for eid in {s.event_id for s in ep.plan.stories}
                    for x in rstore.results(ep.run_id, eid)}
            cites, listed = citations(ep, srcs)
        from ..audio.service import audio_matches
        audio = astore.render(ep.run_id) if ep else None
        audio_stale = audio is not None and not audio_matches(audio, ep)
        if audio_stale:
            audio = None                 # never play audio of an older version of the script
        starts = {ln.line_id: ln.start_s for ln in audio.lines} if audio and audio.status == "complete" else {}
        return page(request, "episode.html", active="episode", ep=ep, cites=cites, sources_list=listed,
                    audio=audio, starts=starts, audio_stale=audio_stale,
                    titles={s.event_id: s.title for s in ep.plan.stories} if ep else {},
                    history=estore.episodes(15))

    @app.get("/media/{run_id}/{name}")
    def media(run_id: str, name: str):
        """Serve a run's final episode audio (only these two file names; nothing else on disk)."""
        if not RUN_ID_RE.fullmatch(run_id) or name not in ("episode.mp3", "episode.wav"):
            raise HTTPException(404, "not found")
        path = settings.data_path / "runs" / run_id / "audio" / name
        if not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="audio/mpeg" if name.endswith(".mp3") else "audio/wav")

    @app.get("/api/runs/{run_id}/audio")
    def api_run_audio(run_id: str):
        run_or_404(run_id)
        ar = astore.render(run_id)
        if ar is None:
            raise HTTPException(404, "no audio for this run")
        return ar.model_dump(mode="json")

    @app.get("/forecasts", response_class=HTMLResponse)
    def forecasts_page(request: Request, status: str | None = Query(None, pattern="^(open|resolved)$")):
        return page(request, "forecasts.html", active="forecasts", forecasts=istore.forecasts(status=status),
                    status=status)

    @app.post("/forecasts/{forecast_id}/resolve")
    def forecasts_resolve(forecast_id: str, verdict: str = Form(..., pattern="^(correct|incorrect|partly|unresolvable)$"),
                          outcome: str = Form(..., min_length=3, max_length=1000)):
        try:
            found = istore.resolve_forecast(forecast_id[:32], verdict, outcome)  # never overwrites a recorded outcome
        except ValueError as e:
            raise HTTPException(409, str(e))
        if found is None:
            raise HTTPException(404, "forecast not found")
        return RedirectResponse("/forecasts", status_code=303)

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
        if settings.research.guardian.api_key_env:
            keys[settings.research.guardian.api_key_env] = bool(read_secret(settings.research.guardian.api_key_env))
        return page(request, "settings.html", active="settings", cfg=cfg, now=datetime.now(timezone.utc), keys=keys,
                    placeholder_contact=PLACEHOLDER_CONTACT in settings.app.user_agent)

    return app
