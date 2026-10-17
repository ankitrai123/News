"""All database reads/writes used by pipeline stages, CLI and web UI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, cast, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import (ArticleRow, Database, EventArticleRow, EventRow, EventScoreRow, FeedStateRow, FetchLogRow,
                 EmbeddingCacheRow, LLMCacheRow, LLMCallRow, PipelineRunRow, StageRunRow, utcnow)
from .pipeline.cluster import PriorEvent
from .providers.base import FetchRecord
from .schemas import Article, EventCandidate, RankedEvent

_ARTICLE_FIELDS = ("article_id", "canonical_url", "source_url", "title", "normalized_title", "syndication_group",
                   "description", "source_name", "source_domain", "source_type", "provider", "published_at",
                   "date_source", "discovered_at", "language", "country", "category", "author", "content_access")


def _row_to_article(r: ArticleRow) -> Article:
    return Article(
        article_id=r.article_id, title=r.title, description=r.description, source_name=r.source_name,
        source_domain=r.source_domain, source_url=r.source_url, canonical_url=r.canonical_url,
        published_at=r.published_at, date_source=r.date_source, discovered_at=r.discovered_at,
        language=r.language, country=r.country, category=r.category or [], author=r.author,
        content_access=r.content_access, provider=r.provider, normalized_title=r.normalized_title,
        syndication_group=r.syndication_group, source_type=r.source_type,
    )


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # -- articles ---------------------------------------------------------
    def recent_articles(self, since: datetime) -> list[Article]:
        with self.db.session() as s:
            rows = s.scalars(select(ArticleRow).where(ArticleRow.discovered_at >= since)).all()
            return [_row_to_article(r) for r in rows]

    def save_articles(self, articles: list[Article]) -> int:
        """Insert; silently ignore articles whose canonical URL already exists. Returns rows inserted."""
        if not articles:
            return 0
        inserted = 0
        with self.db.session() as s:
            for a in articles:
                values = {f: getattr(a, f) for f in _ARTICLE_FIELDS}
                stmt = sqlite_insert(ArticleRow).values(**values).on_conflict_do_nothing()
                inserted += s.execute(stmt).rowcount or 0
        return inserted

    def articles_in_window(self, start: datetime, end: datetime, languages: tuple[str, ...] = ("en",)) -> list[Article]:
        """Articles whose publication time (or discovery time when undated) falls in [start, end]."""
        with self.db.session() as s:
            q = select(ArticleRow).where(
                ((ArticleRow.published_at >= start) & (ArticleRow.published_at <= end))
                | (ArticleRow.published_at.is_(None) & (ArticleRow.discovered_at >= start) & (ArticleRow.discovered_at <= end)))
            rows = s.scalars(q).all()
        arts = [_row_to_article(r) for r in rows]
        if languages:
            arts = [a for a in arts if (a.language or "en")[:2] in languages]
        return arts

    def get_articles(self, ids: list[str]) -> dict[str, Article]:
        out: dict[str, Article] = {}
        with self.db.session() as s:
            for i in range(0, len(ids), 500):
                for r in s.scalars(select(ArticleRow).where(ArticleRow.article_id.in_(ids[i:i + 500]))):
                    out[r.article_id] = _row_to_article(r)
        return out

    def count_articles(self) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(ArticleRow)) or 0

    # -- events -------------------------------------------------------------
    def prior_events(self, since: datetime) -> list[PriorEvent]:
        with self.db.session() as s:
            events = s.scalars(select(EventRow).where(EventRow.last_development_at >= since)).all()
            links: dict[str, set[str]] = {}
            if events:
                for ea in s.scalars(select(EventArticleRow).where(EventArticleRow.event_id.in_([e.event_id for e in events]))):
                    links.setdefault(ea.event_id, set()).add(ea.article_id)
            return [PriorEvent(event_id=e.event_id, centroid=e.centroid or {},
                               entities=[f"country:{c.lower()}" for c in e.countries or []] + [f"ent:{x}" for x in e.entities or []],
                               first_seen_at=e.first_seen_at, article_ids=links.get(e.event_id, set()))
                    for e in events]

    def upsert_events(self, run_id: str, candidates: list[EventCandidate], now: datetime) -> None:
        with self.db.session() as s:
            for c in candidates:
                row = s.get(EventRow, c.event_id)
                if row is None:
                    row = EventRow(event_id=c.event_id, first_seen_at=now, first_article_at=c.first_article_at)
                    s.add(row)
                row.title, row.description = c.title, c.description
                row.topic, row.subtopic = c.topic, c.subtopic
                row.countries, row.entities, row.centroid = c.countries, c.entities, c.centroid
                if c.first_article_at and (row.first_article_at is None or c.first_article_at < row.first_article_at):
                    row.first_article_at = c.first_article_at
                if c.latest_article_at and (row.last_development_at is None or c.latest_article_at > row.last_development_at):
                    row.last_development_at = c.latest_article_at
                row.last_run_id, row.updated_at = run_id, now
            s.flush()
            for c in candidates:
                for aid in c.article_ids:
                    s.execute(sqlite_insert(EventArticleRow).values(
                        event_id=c.event_id, article_id=aid, first_run_id=run_id).on_conflict_do_nothing())

    def save_scores(self, run_id: str, ranked: list[RankedEvent]) -> None:
        with self.db.session() as s:
            s.execute(delete(EventScoreRow).where(EventScoreRow.run_id == run_id))
            for r in ranked:
                s.add(EventScoreRow(run_id=run_id, event_id=r.event_id, rank=r.rank, selected=r.selected,
                                    importance=r.importance, payload=r.model_dump(mode="json")))

    def scores_for_run(self, run_id: str, selected_only: bool = False) -> list[RankedEvent]:
        with self.db.session() as s:
            q = select(EventScoreRow).where(EventScoreRow.run_id == run_id)
            if selected_only:
                q = q.where(EventScoreRow.selected.is_(True))
            rows = s.scalars(q.order_by(EventScoreRow.selected.desc(), EventScoreRow.importance.desc())).all()
            return [RankedEvent.model_validate(r.payload) for r in rows]

    def event(self, event_id: str) -> EventRow | None:
        with self.db.session() as s:
            return s.get(EventRow, event_id)

    def event_articles(self, event_id: str) -> list[Article]:
        with self.db.session() as s:
            rows = s.scalars(select(ArticleRow).join(EventArticleRow, EventArticleRow.article_id == ArticleRow.article_id)
                             .where(EventArticleRow.event_id == event_id)).all()
            arts = [_row_to_article(r) for r in rows]
        return sorted(arts, key=lambda a: a.effective_time, reverse=True)

    def event_score_history(self, event_id: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.scalars(select(EventScoreRow).where(EventScoreRow.event_id == event_id)
                             .order_by(EventScoreRow.created_at)).all()
            return [{"run_id": r.run_id, "rank": r.rank, "selected": r.selected, "importance": r.importance,
                     "created_at": r.created_at} for r in rows]

    # -- runs / stages --------------------------------------------------------
    def create_run(self, run_id: str, kind: str, params: dict) -> None:
        with self.db.session() as s:
            s.add(PipelineRunRow(run_id=run_id, kind=kind, status="running", params=params, started_at=utcnow()))

    def get_run(self, run_id: str) -> PipelineRunRow | None:
        with self.db.session() as s:
            return s.get(PipelineRunRow, run_id)

    def set_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        with self.db.session() as s:
            row = s.get(PipelineRunRow, run_id)
            row.status, row.error = status, error
            row.finished_at = utcnow() if status in {"completed", "failed"} else None

    def active_run(self, max_age: timedelta = timedelta(hours=2)) -> PipelineRunRow | None:
        with self.db.session() as s:
            return s.scalars(select(PipelineRunRow).where(PipelineRunRow.status == "running",
                                                          PipelineRunRow.started_at >= utcnow() - max_age)).first()

    def latest_run(self, status: str | None = "completed") -> PipelineRunRow | None:
        with self.db.session() as s:
            q = select(PipelineRunRow).order_by(PipelineRunRow.started_at.desc())
            if status:
                q = q.where(PipelineRunRow.status == status)
            return s.scalars(q).first()

    def list_runs(self, limit: int = 30) -> list[PipelineRunRow]:
        with self.db.session() as s:
            return list(s.scalars(select(PipelineRunRow).order_by(PipelineRunRow.started_at.desc()).limit(limit)))

    def stages(self, run_id: str) -> dict[str, StageRunRow]:
        with self.db.session() as s:
            return {r.stage: r for r in s.scalars(select(StageRunRow).where(StageRunRow.run_id == run_id).order_by(StageRunRow.id))}

    def stage_start(self, run_id: str, stage: str) -> int:
        with self.db.session() as s:
            row = s.scalars(select(StageRunRow).where(StageRunRow.run_id == run_id, StageRunRow.stage == stage)).first()
            if row is None:
                row = StageRunRow(run_id=run_id, stage=stage, attempts=0)
                s.add(row)
            row.status, row.error, row.started_at, row.finished_at = "running", None, utcnow(), None
            row.attempts += 1
            s.flush()
            return row.attempts

    def stage_finish(self, run_id: str, stage: str, status: str, output: dict | None = None, error: str | None = None) -> None:
        with self.db.session() as s:
            row = s.scalars(select(StageRunRow).where(StageRunRow.run_id == run_id, StageRunRow.stage == stage)).one()
            row.status, row.output, row.error, row.finished_at = status, output, error, utcnow()

    # -- provider fetches / feed health ---------------------------------------
    def log_fetches(self, run_id: str | None, fetches: list[FetchRecord], feed_health: bool = True) -> None:
        now = utcnow()
        with self.db.session() as s:
            for f in fetches:
                s.add(FetchLogRow(run_id=run_id, provider=f.provider, target=f.target, started_at=f.started_at,
                                  duration_ms=f.duration_ms, status_code=f.status_code, ok=f.ok,
                                  item_count=f.item_count, error=(f.error or None) and f.error[:1000]))
                if f.provider != "rss" or not feed_health:
                    continue
                st = s.get(FeedStateRow, f.target) or FeedStateRow(url=f.target, consecutive_failures=0)
                st.last_status, st.last_checked_at = f.status_code, now
                if f.ok:
                    st.consecutive_failures, st.last_error, st.last_ok_at = 0, None, now
                    if not f.not_modified:
                        st.etag, st.last_modified = f.etag, f.last_modified
                        st.last_item_count = f.item_count
                        st.newest_item_at = f.newest_item_at or st.newest_item_at
                else:
                    st.consecutive_failures = (st.consecutive_failures or 0) + 1
                    st.last_error = (f.error or "")[:500]
                s.merge(st)

    def feed_validators(self, url: str) -> tuple[str | None, str | None]:
        with self.db.session() as s:
            st = s.get(FeedStateRow, url)
            return (st.etag, st.last_modified) if st else (None, None)

    def feed_states(self) -> dict[str, FeedStateRow]:
        with self.db.session() as s:
            return {r.url: r for r in s.scalars(select(FeedStateRow))}

    def fetch_log(self, run_id: str | None = None, limit: int = 200) -> list[FetchLogRow]:
        with self.db.session() as s:
            q = select(FetchLogRow).order_by(FetchLogRow.started_at.desc()).limit(limit)
            if run_id:
                q = q.where(FetchLogRow.run_id == run_id)
            return list(s.scalars(q))

    def provider_usage(self, since: datetime | None = None) -> list[dict]:
        """Requests per provider in the last 24h (or since) - for free-tier quota awareness."""
        since = since or datetime.now(timezone.utc) - timedelta(days=1)
        with self.db.session() as s:
            rows = s.execute(select(FetchLogRow.provider, func.count(), func.sum(cast(FetchLogRow.ok, Integer)),
                                    func.sum(FetchLogRow.item_count))
                             .where(FetchLogRow.started_at >= since).group_by(FetchLogRow.provider)).all()
            return [{"provider": p, "requests": n, "ok": int(ok or 0), "items": int(items or 0)} for p, n, ok, items in rows]

    def requests_today(self, provider: str) -> int:
        """Requests that reached `provider` since 00:00 UTC (they may have used quota).
        Requests that never got an HTTP status (network failures) are not counted."""
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(FetchLogRow).where(
                FetchLogRow.provider == provider, FetchLogRow.started_at >= start,
                FetchLogRow.status_code.is_not(None))) or 0

    def unlink_articles(self, event_id: str, article_ids: list[str]) -> None:
        if not article_ids:
            return
        with self.db.session() as s:
            s.execute(delete(EventArticleRow).where(EventArticleRow.event_id == event_id,
                                                    EventArticleRow.article_id.in_(article_ids)))

    # -- LLM ------------------------------------------------------------------
    def llm_cache_get(self, key: str) -> str | None:
        with self.db.session() as s:
            row = s.get(LLMCacheRow, key)
            return row.response if row else None

    def llm_cache_put(self, key: str, provider: str, model: str, response: str) -> None:
        with self.db.session() as s:
            s.merge(LLMCacheRow(key=key, provider=provider, model=model, response=response, created_at=utcnow()))

    def log_llm_call(self, **kw) -> None:
        if kw.get("error"):
            kw["error"] = str(kw["error"])[:1000]
        with self.db.session() as s:
            s.add(LLMCallRow(**kw))

    def llm_usage(self, since: datetime | None = None) -> list[dict]:
        since = since or datetime.now(timezone.utc) - timedelta(days=1)
        with self.db.session() as s:
            rows = s.execute(select(LLMCallRow.provider, LLMCallRow.model, func.count(),
                                    func.sum(cast(LLMCallRow.ok, Integer)), func.sum(cast(LLMCallRow.cached, Integer)),
                                    func.sum(LLMCallRow.prompt_tokens), func.sum(LLMCallRow.completion_tokens))
                             .where(LLMCallRow.started_at >= since)
                             .group_by(LLMCallRow.provider, LLMCallRow.model)).all()
            return [{"provider": p, "model": m, "calls": n, "ok": int(ok or 0), "cached": int(c or 0),
                     "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0)}
                    for p, m, n, ok, c, pt, ct in rows]

    def llm_calls(self, run_id: str | None = None, limit: int = 100) -> list[LLMCallRow]:
        with self.db.session() as s:
            q = select(LLMCallRow).order_by(LLMCallRow.started_at.desc()).limit(limit)
            if run_id:
                q = q.where(LLMCallRow.run_id == run_id)
            return list(s.scalars(q))

    # -- embeddings cache -------------------------------------------------------
    def embeddings_get(self, keys: list[str]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        with self.db.session() as s:
            for i in range(0, len(keys), 500):
                for r in s.scalars(select(EmbeddingCacheRow).where(EmbeddingCacheRow.key.in_(keys[i:i + 500]))):
                    out[r.key] = r.vector
        return out

    def embeddings_put(self, model: str, items: dict[str, bytes]) -> None:
        with self.db.session() as s:
            for k, v in items.items():
                s.merge(EmbeddingCacheRow(key=k, model=model, vector=v, created_at=utcnow()))

    def embeddings_prune(self, older_than: datetime) -> int:
        with self.db.session() as s:
            return s.execute(delete(EmbeddingCacheRow).where(EmbeddingCacheRow.created_at < older_than)).rowcount or 0
