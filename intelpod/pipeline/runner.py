"""Phase 1 pipeline: ingest -> cluster -> rank -> report.

Each stage persists its output in `stage_runs`. Re-running with `resume=True`
skips completed stages and reuses their outputs (and the run's original `now`,
so the time window is identical). A stage that raises is retried once for
transient errors; `StageError` is a deliberate, non-retryable failure.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

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
from .review import drop_outliers, pick_events_to_review, run_reviews
from .report import write_report

log = logging.getLogger(__name__)

PHASE1_STAGES = ("ingest", "cluster", "rank", "review", "report")
DEDUP_MEMORY = timedelta(days=4)
_run_lock = threading.Lock()


class StageError(Exception):
    """Deliberate stage failure that retrying cannot fix (e.g. every source unreachable)."""


class RunInProgress(Exception):
    pass


@dataclass
class RunContext:
    run_id: str
    now: datetime
    params: dict
    outputs: dict[str, dict] = field(default_factory=dict)


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
        self.stage_fns: dict[str, Callable[[RunContext], dict]] = {
            "ingest": self.stage_ingest, "cluster": self.stage_cluster,
            "rank": self.stage_rank, "review": self.stage_review, "report": self.stage_report,
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
        if resume:
            row = self.repo.get_run(run_id) if run_id else self.repo.latest_run(status=None)
            if row is None:
                raise ValueError(f"no run to resume ({run_id or 'none found'})")
            run_id = row.run_id
            ctx = RunContext(run_id=run_id, now=datetime.fromisoformat(row.params["now"]), params=row.params)
            self.repo.set_run_status(run_id, "running")
            log.info("resuming run %s (window ending %s)", run_id, ctx.now.isoformat())
        else:
            active = self.repo.active_run()
            if active and not force:
                raise RunInProgress(f"run {active.run_id} is still marked running; use --force to override")
            now = now or datetime.now(timezone.utc)
            run_id = run_id or f"run_{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
            params = {"now": now.isoformat(), "lookback_hours": self.settings.ingestion.lookback_hours,
                      "stages": list(PHASE1_STAGES)}
            self.repo.create_run(run_id, "phase1", params)
            ctx = RunContext(run_id=run_id, now=now, params=params)
            log.info("started run %s", run_id)

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
            out["duration_s"] = round(time.monotonic() - t0, 2)
            self.repo.stage_finish(ctx.run_id, stage, "completed", output=out)
            log.info("[%s] %s completed in %.1fs", ctx.run_id, stage, out["duration_s"])
            return out
        raise RuntimeError("unreachable")

    # -- providers --------------------------------------------------------------
    def build_providers(self, http: HttpClient) -> list[NewsProvider]:
        cfg = self.settings.ingestion
        feeds = load_feeds(self.settings.resolve(cfg.feeds_file))
        domain_publishers = {f.domain: f.publisher for f in feeds if f.domain}
        providers: list[NewsProvider] = []
        if cfg.providers.rss.enabled and feeds:
            providers.append(RssProvider(http, feeds, validators=self.repo.feed_validators,
                                         max_description_chars=cfg.max_description_chars))
        if cfg.providers.gdelt.enabled and cfg.providers.gdelt.queries:
            providers.append(GdeltProvider(http, cfg.providers.gdelt, domain_publishers))
        nd = cfg.providers.newsdata
        if nd.enabled and nd.queries:
            providers.append(NewsdataProvider(http, nd, credits_used_today=lambda: self.repo.requests_today("newsdata"),
                                              domain_publishers=domain_publishers,
                                              max_description_chars=cfg.max_description_chars))
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
        try:
            for p in providers:
                try:
                    res = p.fetch()
                except Exception as e:  # noqa: BLE001 - a broken provider must not sink the run
                    log.exception("provider %s crashed", p.name)
                    res_fetches = [FetchRecord(provider=p.name, target="(provider)", started_at=ctx.now,
                                               duration_ms=0, ok=False, error=f"{type(e).__name__}: {e}")]
                    self.repo.log_fetches(ctx.run_id, res_fetches)
                    stats[p.name] = {"requests": 1, "ok": 0, "items": 0, "errors": [str(e)[:200]]}
                    continue
                self.repo.log_fetches(ctx.run_id, res.fetches)
                skipped = getattr(p, "skipped_reason", None)
                normalized = [a for it in res.items
                              if (a := normalize_item(it, now=datetime.now(timezone.utc),
                                                      max_description_chars=cfg.ingestion.max_description_chars))]
                all_new.extend(normalized)
                stats[p.name] = {
                    "requests": len(res.fetches), "ok": sum(f.ok for f in res.fetches),
                    "not_modified": sum(f.not_modified for f in res.fetches), "items": len(res.items),
                    "normalized": len(normalized),
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

        existing = self.repo.recent_articles(ctx.now - DEDUP_MEMORY)
        kept, dstats = deduplicate(all_new, existing)
        inserted = self.repo.save_articles(kept)
        ok_requests = sum(s["ok"] for s in stats.values())
        window = self._window(ctx)
        in_window = len(self.repo.articles_in_window(*window))
        out = {"providers": stats, "dedup": dstats.as_dict(), "inserted": inserted,
               "articles_in_window": in_window, "warnings": []}
        if ok_requests == 0:
            msg = "every provider request failed (network blocked or sources down) - see fetch log"
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
        if embedder is None and ccfg.embeddings.enabled:
            embedder = self._make_embedder()
        docs = build_docs(articles, self.taxonomy, embedder)
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
        prov = self.settings.llm.providers.get("nvidia")
        if prov is None or not prov.api_key_env:
            return None
        return CachedEmbedder(NvidiaEmbedder(ecfg, prov.base_url, prov.api_key_env,
                                             requests_per_minute=prov.requests_per_minute), self.repo)

    def _make_llm(self, run_id: str):
        if self._llm_client_factory is not None:
            return self._llm_client_factory(run_id)
        return LLMClient(self.settings.llm, repo=self.repo, run_id=run_id, user_agent=self.settings.app.user_agent)

    def stage_review(self, ctx: RunContext) -> dict:
        """Optional model review of top events; never fails the run for provider problems."""
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
        ranked = self.repo.scores_for_run(ctx.run_id)
        targets = pick_events_to_review(ranked, rcfg.max_events)
        articles = self.repo.get_articles(sorted({a for c in candidates.values() for a in c.article_ids}))
        reviews, errors = run_reviews(client, targets, candidates, articles)
        for eid, rev in reviews.items():
            if rev.outlier_article_ids:
                candidates[eid] = drop_outliers(candidates[eid], rev, articles)
                self.repo.unlink_articles(eid, rev.outlier_article_ids)
        if reviews:
            reranked = Ranker(self.settings.ranking, self.taxonomy).rank(
                list(candidates.values()), articles, reviews, rcfg.weight)
            self.repo.save_scores(ctx.run_id, reranked)
            selected = [r for r in reranked if r.selected]
        else:
            selected = [r for r in ranked if r.selected]
        out.update({
            "status": "completed" if reviews else "failed_all",
            "provider": client.provider, "model": next((r.model for r in reviews.values()), None),
            "requested": len(targets), "reviewed": len(reviews), "errors": errors[:20],
            "outliers_removed": sum(len(r.outlier_article_ids) for r in reviews.values()),
            "titles_rejected": sum(1 for r in reviews.values() if "title" in r.rejected),
            "summaries_rejected": sum(1 for r in reviews.values() if "summary" in r.rejected),
            "selected_event_ids": [r.event_id for r in selected],
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

    def stage_report(self, ctx: RunContext) -> dict:
        ranked = self.repo.scores_for_run(ctx.run_id)
        selected = [r for r in ranked if r.selected]
        cand_list = (ctx.outputs.get("review") or {}).get("candidates") or ctx.outputs["cluster"]["candidates"]
        articles = self.repo.get_articles(sorted({a for c in cand_list for a in c["article_ids"]}))
        cands = {c["event_id"]: c for c in cand_list}
        out_dir = self.settings.data_path / "runs" / ctx.run_id
        paths = write_report(out_dir, ctx, selected, ranked, cands, articles)
        return {"paths": {k: str(v) for k, v in paths.items()}}


def selected_events(repo: Repository, run_id: str) -> list[RankedEvent]:
    return repo.scores_for_run(run_id, selected_only=True)
