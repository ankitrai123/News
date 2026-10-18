"""Phase 1 pipeline: ingest -> cluster -> rank -> report.

Each stage persists its output in `stage_runs`. Re-running with `resume=True`
skips completed stages and reuses their outputs (and the run's original `now`,
so the time window is identical). A stage that raises is retried once for
transient errors; `StageError` is a deliberate, non-retryable failure.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import __version__
from ..config import Settings
from ..db import Database
from ..http import HttpClient
from ..llm.client import LLMClient
from ..providers import GdeltProvider, NewsProvider, RssProvider, load_feeds
from ..providers.newsdata import NewsdataProvider
from ..providers.base import FetchRecord
from ..repository import Repository
from ..schemas import EventCandidate, RankedEvent
from ..textutil import Taxonomy, load_taxonomy
from .cluster import Clusterer, build_candidates, build_docs
from .dedup import deduplicate
from .embeddings import CachedEmbedder, NvidiaEmbedder, OllamaEmbedder
from .normalize import normalize_item
from .rank import Ranker
from .review import drop_outliers, pick_events_to_review, regroup_outliers, run_reviews
from .report import write_report

log = logging.getLogger(__name__)

STAGES = ("ingest", "cluster", "rank", "review", "research", "debate", "impact", "script", "report")
PHASE1_STAGES = STAGES  # name kept for callers written before Phase 2
DEDUP_MEMORY = timedelta(days=4)
API_USER_AGENT = f"IntelPod/{__version__}"  # keyed APIs get no contact details (privacy)
_run_lock = threading.Lock()


class StageError(Exception):
    """Deliberate stage failure that retrying cannot fix (e.g. every source unreachable)."""


class RunInProgress(Exception):
    pass


class NothingToResume(ValueError):
    """The run asked to resume is already complete (or there is no run)."""


RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
RESUMABLE = ("failed", "paused")


@dataclass
class RunContext:
    run_id: str
    now: datetime
    params: dict
    outputs: dict[str, dict] = field(default_factory=dict)


def feed_pause_state(st, cfg, now: datetime | None = None) -> str | None:
    """Why a feed is skipped this run ("robots.txt" / "failing"), or None if it is fetched.
    A paused feed is retried once every `feed_retry_paused_after_hours`."""
    if st is None:
        return None
    now = now or datetime.now(timezone.utc)
    robots = "robots.txt" in (st.last_error or "")
    failing = (st.consecutive_failures or 0) >= cfg.feed_pause_after_failures
    due = st.last_checked_at is None or now - st.last_checked_at >= timedelta(hours=cfg.feed_retry_paused_after_hours)
    if due or not (robots or failing):
        return None
    return "robots.txt" if robots else "failing"


class Phase1Pipeline:
    def __init__(self, settings: Settings, db: Database | None = None,
                 providers: list[NewsProvider] | None = None,
                 embedder: Callable[[list[str]], list[list[float]] | None] | None = None,
                 taxonomy: Taxonomy | None = None, stage_retry_delay: float = 1.0,
                 llm_client_factory: Callable[[str], object] | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_file)
        self.repo = Repository(self.db)
        self._providers = providers
        self._embedder = embedder
        self.taxonomy = taxonomy or load_taxonomy(settings.resolve(settings.taxonomy_file))
        self.stage_retry_delay = stage_retry_delay
        self._llm_client_factory = llm_client_factory
        self._extra_clients: list[HttpClient] = []
        self._provider_notes: dict[str, str] = {}
        self.stage_fns: dict[str, Callable[[RunContext], dict]] = {
            "ingest": self.stage_ingest, "cluster": self.stage_cluster,
            "rank": self.stage_rank, "review": self.stage_review, "research": self.stage_research,
            "debate": self.stage_debate, "impact": self.stage_impact,
            "script": self.stage_script, "report": self.stage_report,
        }

    # -- orchestration ------------------------------------------------------
    def run(self, run_id: str | None = None, resume: bool = False, now: datetime | None = None,
            stop_after: str | None = None, force: bool = False) -> str:
        if not _run_lock.acquire(blocking=False):
            raise RunInProgress("another run is in progress in this process")
        try:
            return self._run(run_id, resume, now, stop_after, force)
        finally:
            _run_lock.release()

    def _run(self, run_id, resume, now, stop_after, force) -> str:
        if run_id is not None and not RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"invalid run id {run_id!r}")
        active = self.repo.active_run()  # also marks runs whose process has ended as interrupted
        if resume:
            row = self.repo.get_run(run_id) if run_id else self.repo.latest_run(status=None)
            if row is None:
                raise NothingToResume(f"no run to resume ({run_id or 'no runs yet'})")
            if row.status == "completed":
                raise NothingToResume(f"nothing to resume: run {row.run_id} already completed; start a new run instead")
            if active is not None and not force:
                raise RunInProgress(f"run {active.run_id} is in progress in another process; wait for it, or use "
                                    f"--force if you are sure it is not running")
            run_id = row.run_id
            ctx = RunContext(run_id=run_id, now=datetime.fromisoformat(row.params["now"]), params=row.params)
            self.repo.set_run_status(run_id, "running")
            log.info("resuming run %s (window ending %s)", run_id, ctx.now.isoformat())
        else:
            if active and not force:
                raise RunInProgress(f"run {active.run_id} is still marked running; use --force to override")
            now = now or datetime.now(timezone.utc)
            run_id = run_id or f"run_{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
            params = {"now": now.isoformat(), "lookback_hours": self.settings.ingestion.lookback_hours,
                      "stages": list(PHASE1_STAGES)}
            self.repo.create_run(run_id, "phase1", params)
            ctx = RunContext(run_id=run_id, now=now, params=params)
            log.info("started run %s", run_id)

        try:
            return self._run_stages(ctx, stop_after)
        except BaseException as e:  # Ctrl-C / shutdown: leave the run resumable, not "running"
            if not isinstance(e, Exception):
                self.repo.set_run_status(ctx.run_id, "failed", "interrupted; resume to continue")
            raise

    def _run_stages(self, ctx: RunContext, stop_after: str | None) -> str:
        run_id = ctx.run_id
        done = self.repo.stages(run_id)
        for stage in PHASE1_STAGES:
            prev = done.get(stage)
            if prev and prev.status == "completed":
                ctx.outputs[stage] = prev.output or {}
                log.info("[%s] %s already completed - reusing persisted output", run_id, stage)
            else:
                try:
                    ctx.outputs[stage] = self._run_stage(ctx, stage)
                except Exception as e:  # noqa: BLE001 - recorded, then re-raised
                    self.repo.set_run_status(run_id, "failed", f"{stage}: {e}")
                    raise
            if stop_after == stage:
                last = stage == PHASE1_STAGES[-1]
                self.repo.set_run_status(run_id, "completed" if last else "paused",
                                         None if last else f"stopped after {stage}; resume with --resume")
                return run_id
        self.repo.set_run_status(run_id, "completed")
        log.info("run %s completed", run_id)
        return run_id

    def _run_stage(self, ctx: RunContext, stage: str, max_attempts: int = 2) -> dict:
        fn = self.stage_fns[stage]
        for attempt in range(1, max_attempts + 1):
            self.repo.stage_start(ctx.run_id, stage)
            t0 = time.monotonic()
            try:
                out = fn(ctx)
            except StageError as e:
                self.repo.stage_finish(ctx.run_id, stage, "failed", error=str(e))
                log.error("[%s] %s failed: %s", ctx.run_id, stage, e)
                raise
            except Exception as e:  # noqa: BLE001
                self.repo.stage_finish(ctx.run_id, stage, "failed", error=f"{type(e).__name__}: {e}")
                log.exception("[%s] %s attempt %d/%d failed", ctx.run_id, stage, attempt, max_attempts)
                if attempt == max_attempts:
                    raise
                time.sleep(self.stage_retry_delay)
                continue
            except BaseException:  # Ctrl-C / shutdown: the stage must not stay "running"
                self.repo.stage_finish(ctx.run_id, stage, "failed", error="interrupted; resume to continue")
                raise
            out["duration_s"] = round(time.monotonic() - t0, 2)
            self.repo.stage_finish(ctx.run_id, stage, "completed", output=out)
            log.info("[%s] %s completed in %.1fs", ctx.run_id, stage, out["duration_s"])
            return out
        raise RuntimeError("unreachable")

    # -- providers --------------------------------------------------------------
    def _active_feeds(self, feeds):
        """Skip feeds that keep failing (or whose robots.txt forbids access); retry each paused
        feed once every `feed_retry_paused_after_hours` so a fixed feed comes back by itself."""
        cfg = self.settings.ingestion
        states = self.repo.feed_states()
        now = datetime.now(timezone.utc)
        active, paused = [], []
        for f in feeds:
            if feed_pause_state(states.get(f.url), cfg, now):
                paused.append(f.name)
            else:
                active.append(f)
        if feeds and not active:
            # Every feed paused at once is far more likely a local outage than 20 broken feeds.
            return list(feeds), []
        return active, paused

    def build_providers(self, http: HttpClient) -> list[NewsProvider]:
        cfg = self.settings.ingestion
        all_feeds = load_feeds(self.settings.resolve(cfg.feeds_file))
        domain_publishers = {f.domain: f.publisher for f in all_feeds if f.domain}
        feeds, paused = self._active_feeds(all_feeds)
        if paused:
            self._provider_notes["rss"] = (f"{len(paused)} feed(s) paused after repeated failures or robots.txt refusal: "
                                           + ", ".join(paused[:8]) + ("…" if len(paused) > 8 else ""))
        providers: list[NewsProvider] = []
        if cfg.providers.rss.enabled and feeds:
            providers.append(RssProvider(http, feeds, validators=self.repo.feed_validators,
                                         max_description_chars=cfg.max_description_chars,
                                         max_workers=cfg.providers.rss.max_workers))
        if cfg.providers.gdelt.enabled and cfg.providers.gdelt.queries:
            from ..providers import gdelt_backoff
            until = gdelt_backoff.blocked_until(self.db)
            if until:
                self._provider_notes["gdelt"] = gdelt_backoff.reason(until)
            else:
                providers.append(GdeltProvider(http, cfg.providers.gdelt, domain_publishers))
        nd = cfg.providers.newsdata
        if nd.enabled and nd.queries:
            api_http = HttpClient(self.settings.http, API_USER_AGENT)
            self._extra_clients.append(api_http)
            providers.append(NewsdataProvider(api_http, nd, credits_used_today=lambda: self.repo.requests_today("newsdata"),
                                              domain_publishers=domain_publishers,
                                              max_description_chars=cfg.max_description_chars))
        elif nd.enabled:
            self._provider_notes["newsdata"] = "enabled but no queries configured (ingestion.providers.newsdata.queries)"
        return providers

    # -- stages ---------------------------------------------------------------------
    def stage_ingest(self, ctx: RunContext) -> dict:
        cfg = self.settings
        http = None
        providers = self._providers
        if providers is None:
            http = HttpClient(cfg.http, cfg.app.user_agent)
            providers = self.build_providers(http)
        if not providers:
            raise StageError("no news providers enabled; enable rss and/or gdelt in config")
        stats: dict[str, dict] = {}
        all_new = []

        def fetch_one(p):
            t0 = time.monotonic()
            try:
                return p.fetch(), None, time.monotonic() - t0
            except Exception as e:  # noqa: BLE001 - a broken provider must not sink the run
                log.exception("provider %s crashed", p.name)
                return None, e, time.monotonic() - t0

        try:
            # Providers talk to different hosts, so they run concurrently: a slow or throttled
            # GDELT no longer adds its time to RSS and NewsData. Results are processed in order.
            with ThreadPoolExecutor(max_workers=max(1, len(providers)), thread_name_prefix="ingest") as pool:
                fetched = list(pool.map(fetch_one, providers))
            for p, (res, err, took) in zip(providers, fetched):
                if err is not None:
                    e = err
                    res_fetches = [FetchRecord(provider=p.name, target="(provider)", started_at=ctx.now,
                                               duration_ms=int(took * 1000), ok=False, error=f"{type(e).__name__}: {e}")]
                    self.repo.log_fetches(ctx.run_id, res_fetches)
                    stats[p.name] = {"requests": 1, "ok": 0, "items": 0, "errors": [str(e)[:200]]}
                    continue
                # If every feed failed, the problem is almost certainly our network, not 20 feeds:
                # log the requests but do not count them toward pausing feeds.
                all_failed = p.name == "rss" and len(res.fetches) > 1 and not any(f.ok for f in res.fetches)
                self.repo.log_fetches(ctx.run_id, res.fetches, feed_health=not all_failed)
                if p.name == "gdelt":
                    from ..providers import gdelt_backoff
                    gdelt_backoff.note(self.db, res.fetches)
                skipped = getattr(p, "skipped_reason", None)
                normalized = [a for it in res.items
                              if (a := normalize_item(it, now=datetime.now(timezone.utc),
                                                      max_description_chars=cfg.ingestion.max_description_chars))]
                all_new.extend(normalized)
                stats[p.name] = {
                    "requests": len(res.fetches), "ok": sum(f.ok for f in res.fetches),
                    "not_modified": sum(f.not_modified for f in res.fetches), "items": len(res.items),
                    "normalized": len(normalized), "seconds": round(took, 1),
                    "errors": [f"{f.target}: {f.error}" for f in res.fetches if not f.ok][:20],
                }
                if skipped:
                    stats[p.name]["note"] = skipped
                credits = getattr(p, "last_credits_remaining", None)
                if credits is not None:
                    stats[p.name]["credits_remaining"] = credits
        finally:
            if http:
                http.close()
            for c in self._extra_clients:
                c.close()
            self._extra_clients.clear()
        for name, note in self._provider_notes.items():
            stats.setdefault(name, {"requests": 0, "ok": 0, "items": 0, "normalized": 0, "errors": []})["note"] = note

        existing = self.repo.recent_articles(ctx.now - DEDUP_MEMORY)
        kept, dstats = deduplicate(all_new, existing)
        inserted = self.repo.save_articles(kept)
        ok_requests = sum(s["ok"] for s in stats.values())
        requests = sum(s["requests"] for s in stats.values())
        window = self._window(ctx)
        in_window = len(self.repo.articles_in_window(*window))
        out = {"providers": stats, "dedup": dstats.as_dict(), "inserted": inserted,
               "articles_in_window": in_window, "warnings": []}
        if ok_requests == 0 and not all_new:
            msg = ("every provider request failed (network blocked or sources down) - see fetch log" if requests
                   else "no source was fetched (all feeds disabled, paused or skipped) - see Sources")
            if in_window == 0:
                raise StageError(msg)
            out["warnings"].append(msg + "; continuing with previously stored articles (stale data)")
        return out

    def _window(self, ctx: RunContext) -> tuple[datetime, datetime]:
        return ctx.now - timedelta(hours=ctx.params.get("lookback_hours", self.settings.ingestion.lookback_hours)), \
            ctx.now + timedelta(hours=1)

    def stage_cluster(self, ctx: RunContext) -> dict:
        ccfg = self.settings.clustering
        articles = self.repo.articles_in_window(*self._window(ctx))
        if not articles:
            raise StageError("no articles in the lookback window; run ingestion first")
        embedder = self._embedder
        embed_note = "disabled"
        self._embed_skip_reason = None
        if embedder is None and ccfg.embeddings.enabled:
            embedder = self._make_embedder()
            if embedder is None and self._embed_skip_reason:
                embed_note = self._embed_skip_reason
        docs = build_docs(articles, self.taxonomy, embedder, ccfg)
        if embedder is not None:
            if any(d.dense for d in docs):
                embed_note = f"used ({getattr(embedder, 'model', '?')}; {getattr(embedder, 'hits', 0)} cached, " \
                             f"{getattr(embedder, 'misses', 0)} new)"
            else:
                embed_note = f"unavailable ({getattr(embedder, 'last_error', None) or 'unknown'}); lexical similarity only"
        clusters = Clusterer(ccfg).cluster(docs)
        prior = self.repo.prior_events(ctx.now - timedelta(days=ccfg.link_lookback_days))
        candidates = build_candidates(clusters, self.taxonomy, ccfg, prior)
        self.repo.upsert_events(ctx.run_id, candidates, ctx.now)
        multi = [c for c in candidates if len(c.article_ids) > 1]
        return {
            "articles": len(articles), "events": len(candidates), "multi_article_events": len(multi),
            "continuing_events": sum(c.is_continuing for c in candidates), "embeddings": embed_note,
            "candidates": [c.model_dump(mode="json") for c in candidates],
        }

    def _make_embedder(self):
        ecfg = self.settings.clustering.embeddings
        if ecfg.provider == "ollama":
            return CachedEmbedder(OllamaEmbedder(ecfg), self.repo)
        if not self.settings.llm.enabled:
            # llm.enabled=false / --no-llm means "nothing sent to a hosted model", embeddings included.
            self._embed_skip_reason = "NVIDIA embeddings off because llm.enabled is false (--no-llm)"
            return None
        prov = self.settings.llm.providers.get("nvidia")
        if prov is None or not prov.api_key_env:
            return None
        return CachedEmbedder(NvidiaEmbedder(ecfg, prov.base_url, prov.api_key_env,
                                             requests_per_minute=prov.requests_per_minute,
                                             backoff_base_seconds=prov.backoff_base_seconds), self.repo)

    def _make_llm(self, run_id: str):
        if self._llm_client_factory is not None:
            return self._llm_client_factory(run_id)
        return LLMClient(self.settings.llm, repo=self.repo, run_id=run_id, user_agent=API_USER_AGENT)

    def stage_review(self, ctx: RunContext) -> dict:
        """Optional model review of top events; never fails the run for provider problems.

        Pass 1 reviews the highest-ranked events. Re-ranking with blended scores can promote
        events that were not reviewed into the briefing; those get follow-up reviews (bounded
        by `max_extra_reviews`), and any still unreviewed are flagged. Reports the model calls
        off-topic are removed from their event and regrouped into events of their own."""
        rcfg = self.settings.llm.event_review
        candidates = {c["event_id"]: EventCandidate.model_validate(c) for c in ctx.outputs["cluster"]["candidates"]}
        out: dict = {"status": "skipped", "reviewed": 0, "candidates": None}
        if not (self.settings.llm.enabled and rcfg.enabled and rcfg.max_events > 0):
            out["reason"] = "disabled in config (llm.enabled / llm.event_review)"
            return out
        client = self._make_llm(ctx.run_id)
        ok, why = client.availability()
        if not ok:
            out["reason"] = why
            log.info("review skipped: %s", why)
            return out
        ranker = Ranker(self.settings.ranking, self.taxonomy)
        ranked = self.repo.scores_for_run(ctx.run_id)
        articles = self.repo.get_articles(sorted({a for c in candidates.values() for a in c.article_ids}))
        reviews: dict = {}
        errors: list[dict] = []
        attempted: set[str] = set()
        regrouped: list[str] = []
        targets = pick_events_to_review(ranked, rcfg.max_events)
        extra_left = rcfg.max_extra_reviews
        passes = 0
        while targets:
            passes += 1
            attempted.update(targets)
            new_reviews, new_errors = run_reviews(client, targets, candidates, articles)
            reviews.update(new_reviews)
            errors.extend(new_errors)
            for eid, rev in new_reviews.items():
                if not rev.outlier_article_ids:
                    continue
                removed = [articles[a] for a in rev.outlier_article_ids if a in articles]
                candidates[eid] = drop_outliers(candidates[eid], rev, articles, self.taxonomy, self.settings.clustering)
                self.repo.unlink_articles(eid, rev.outlier_article_ids)
                fresh = regroup_outliers(removed, self.taxonomy, self.settings.clustering)
                fresh = [c for c in fresh if c.event_id not in candidates]
                if fresh:
                    self.repo.upsert_events(ctx.run_id, fresh, ctx.now)
                    for c in fresh:
                        candidates[c.event_id] = c
                        regrouped.append(c.event_id)
            if any(e.get("fatal") for e in new_errors) or not reviews:
                break
            ranked = ranker.rank(list(candidates.values()), articles, reviews, rcfg.weight)
            pending = [r.event_id for r in ranked if r.selected and r.event_id not in attempted
                       and "soft_news" not in r.flags]
            targets = pending[:extra_left]
            extra_left -= len(targets)
        if reviews:
            final = ranker.rank(list(candidates.values()), articles, reviews, rcfg.weight)
            for r in final:
                if r.selected and r.review is None:
                    r.flags.append("not_model_reviewed")
                    r.rationale += " Not reviewed by the model (review budget or provider limit reached)."
            self.repo.save_scores(ctx.run_id, final)
            selected = [r for r in final if r.selected]
        else:
            selected = [r for r in ranked if r.selected]
        out.update({
            "status": "completed" if reviews else "failed_all",
            "provider": client.provider, "model": next((r.model for r in reviews.values()), None),
            "models": sorted({r.model for r in reviews.values()}),
            "requested": len(attempted), "reviewed": len(reviews), "passes": passes, "errors": errors[:20],
            "outliers_removed": sum(len(r.outlier_article_ids) for r in reviews.values()),
            "regrouped_events": regrouped,
            "titles_rejected": sum(1 for r in reviews.values() if "title" in r.rejected),
            "summaries_rejected": sum(1 for r in reviews.values() if "summary" in r.rejected),
            "selected_event_ids": [r.event_id for r in selected],
            "unreviewed_selected": [r.event_id for r in selected if r.review is None] if reviews else [],
            "candidates": [c.model_dump(mode="json") for c in candidates.values()] if reviews else None,
        })
        if not reviews:
            out["reason"] = errors[0]["error"] if errors else "no events to review"
        return out

    def stage_rank(self, ctx: RunContext) -> dict:
        candidates = [EventCandidate.model_validate(c) for c in ctx.outputs["cluster"]["candidates"]]
        ids = sorted({a for c in candidates for a in c.article_ids})
        articles = self.repo.get_articles(ids)
        ranked = Ranker(self.settings.ranking, self.taxonomy).rank(candidates, articles)
        self.repo.save_scores(ctx.run_id, ranked)
        selected = [r for r in ranked if r.selected]
        return {"selected": len(selected), "scored": len(ranked),
                "selected_event_ids": [r.event_id for r in selected],
                "top": [{"rank": r.rank, "event_id": r.event_id, "title": r.title, "importance": r.importance}
                        for r in selected]}

    def stage_research(self, ctx: RunContext) -> dict:
        """Phase 2: a source-backed research package for each selected event (see intelpod.research)."""
        from ..research.runner import run_research  # local import: Phase 1 works without it loaded
        return run_research(self, ctx)

    def stage_debate(self, ctx: RunContext) -> dict:
        """Phase 3: claims, viewpoints, agreement/disagreement and uncertainty (see intelpod.debate)."""
        from ..debate.runner import run_debate
        try:
            return run_debate(self, ctx)
        except Exception as e:  # noqa: BLE001 - Phase 3 is an add-on: the briefing must still be written
            log.exception("[%s] debate stage failed", ctx.run_id)
            return {"status": "failed", "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    def stage_impact(self, ctx: RunContext) -> dict:
        """Phase 4: context, business/economic impact, connections, forecast ledger (see intelpod.impact)."""
        from ..impact.runner import run_impact
        try:
            return run_impact(self, ctx)
        except Exception as e:  # noqa: BLE001 - the briefing must still be written
            log.exception("[%s] impact stage failed", ctx.run_id)
            return {"status": "failed", "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    def stage_script(self, ctx: RunContext) -> dict:
        """Phase 5: two-host script, verified line by line (see intelpod.script)."""
        from ..script.runner import run_script
        try:
            return run_script(self, ctx)
        except Exception as e:  # noqa: BLE001 - the briefing must still be written
            log.exception("[%s] script stage failed", ctx.run_id)
            return {"status": "failed", "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    def stage_report(self, ctx: RunContext) -> dict:
        ranked = self.repo.scores_for_run(ctx.run_id)
        selected = [r for r in ranked if r.selected]
        cand_list = (ctx.outputs.get("review") or {}).get("candidates") or ctx.outputs["cluster"]["candidates"]
        articles = self.repo.get_articles(sorted({a for c in cand_list for a in c["article_ids"]}))
        cands = {c["event_id"]: c for c in cand_list}
        out_dir = self.settings.data_path / "runs" / ctx.run_id
        from ..research.store import ResearchStore
        store = ResearchStore(self.db)
        research = {r.event_id: (pkg, store.results(ctx.run_id, r.event_id, selected_only=True))
                    for r in selected if (pkg := store.package(ctx.run_id, r.event_id)) is not None}
        from ..debate.store import DebateStore
        debates = DebateStore(self.db).packages(ctx.run_id)
        paths = write_report(out_dir, ctx, selected, ranked, cands, articles, research, debates)
        brief_error = None
        try:  # the intelligence brief is extra: a problem with it must not lose the main report
            from ..impact.report import write_brief
            from ..impact.store import ImpactStore
            istore = ImpactStore(self.db)
            analyses = istore.analyses(ctx.run_id)
            if analyses:
                brief = istore.brief(ctx.run_id)
                new = set(brief.new_forecasts) if brief else set()
                forecasts = [f for f in istore.forecasts() if f.forecast_id in new]
                sources = {eid: {x.result_id: x for x in rs} for eid, (_, rs) in research.items()}
                for eid in analyses:  # sources added by viewpoint searches are in the research store too
                    sources.setdefault(eid, {})
                    for x in store.results(ctx.run_id, eid):
                        sources[eid].setdefault(x.result_id, x)
                paths.update(write_brief(out_dir, ctx.run_id, ctx.now, selected, analyses, debates, sources, brief,
                                         forecasts))
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] intelligence brief could not be written", ctx.run_id)
            brief_error = f"{type(e).__name__}: {str(e)[:300]}"
        try:  # episode files (Phase 5)
            from ..script.report import write_episode
            from ..script.store import EpisodeStore
            ep = EpisodeStore(self.db).episode(ctx.run_id)
            if ep is not None and not ep.lines:   # no script this time: do not leave an older one on disk
                for name in ("script.md", "transcript.json", "episode_notes.md", "evidence_report.md"):
                    (out_dir / name).unlink(missing_ok=True)
            if ep is not None and ep.lines:
                all_sources = {x.result_id: x for eid in {s.event_id for s in ep.plan.stories}
                               for x in store.results(ctx.run_id, eid)}
                paths.update(write_episode(out_dir, ep, all_sources))
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] episode files could not be written", ctx.run_id)
            brief_error = (brief_error + "; " if brief_error else "") + f"episode: {type(e).__name__}: {str(e)[:200]}"
        out = {"paths": {k: str(v) for k, v in paths.items()}}
        if brief_error:
            out["brief_error"] = brief_error
        return out


def selected_events(repo: Repository, run_id: str) -> list[RankedEvent]:
    return repo.scores_for_run(run_id, selected_only=True)
