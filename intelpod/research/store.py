"""Database reads/writes for Phase 2 research (kept apart from the Phase 1 repository)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..db import (Database, PageCacheRow, ResearchItemRow, ResearchPackageRow, ResearchQueryRow, ResearchResultRow,
                  SearchCacheRow, utcnow)
from .models import PageInfo, QueryStatus, ResearchPackage, ResearchResult, SearchHit


class ResearchStore:
    def __init__(self, db: Database):
        self.db = db

    # -- research feed items ------------------------------------------------------
    def save_items(self, rows: list[dict]) -> int:
        """Insert research-feed items; existing canonical URLs are left as they are."""
        inserted = 0
        with self.db.session() as s:
            for r in rows:
                inserted += s.execute(sqlite_insert(ResearchItemRow).values(**r).on_conflict_do_nothing()).rowcount or 0
        return inserted

    def items_in_window(self, start: datetime, end: datetime) -> list[ResearchItemRow]:
        with self.db.session() as s:
            q = select(ResearchItemRow).where(
                ((ResearchItemRow.published_at >= start) & (ResearchItemRow.published_at <= end))
                | (ResearchItemRow.published_at.is_(None) & (ResearchItemRow.discovered_at >= start)
                   & (ResearchItemRow.discovered_at <= end)))
            return list(s.scalars(q))

    def prune_items(self, older_than: datetime) -> int:
        with self.db.session() as s:
            return s.execute(delete(ResearchItemRow).where(ResearchItemRow.discovered_at < older_than,
                                                           (ResearchItemRow.published_at.is_(None))
                                                           | (ResearchItemRow.published_at < older_than))).rowcount or 0

    # -- caches -----------------------------------------------------------------------
    def search_cache_get(self, key: str, max_age: timedelta) -> list[SearchHit] | None:
        with self.db.session() as s:
            row = s.get(SearchCacheRow, key)
            if row is None or row.created_at < utcnow() - max_age:
                return None
            return [SearchHit.model_validate(h) for h in row.hits]

    def search_cache_put(self, key: str, provider: str, hits: list[SearchHit]) -> None:
        with self.db.session() as s:
            s.merge(SearchCacheRow(key=key, provider=provider, hits=[h.model_dump(mode="json") for h in hits],
                                   created_at=utcnow()))

    def page_cache_get(self, canonical_url: str, max_age: timedelta) -> PageInfo | None:
        with self.db.session() as s:
            row = s.get(PageCacheRow, canonical_url)
            if row is None or row.fetched_at < utcnow() - max_age:
                return None
            return PageInfo.model_validate(row.payload)

    def page_cache_put(self, canonical_url: str, page: PageInfo) -> None:
        with self.db.session() as s:
            s.merge(PageCacheRow(canonical_url=canonical_url, status=page.status,
                                 payload=page.model_dump(mode="json"), fetched_at=utcnow()))

    def prune_caches(self, search_older_than: datetime, pages_older_than: datetime) -> None:
        with self.db.session() as s:
            s.execute(delete(SearchCacheRow).where(SearchCacheRow.created_at < search_older_than))
            s.execute(delete(PageCacheRow).where(PageCacheRow.fetched_at < pages_older_than))

    # -- results and packages -------------------------------------------------------
    def save_event_research(self, package: ResearchPackage, results: list[ResearchResult],
                            queries: list[QueryStatus]) -> None:
        """Replace this run's research for one event in one transaction (idempotent on resume)."""
        with self.db.session() as s:
            s.execute(delete(ResearchResultRow).where(ResearchResultRow.run_id == package.run_id,
                                                      ResearchResultRow.event_id == package.event_id))
            s.execute(delete(ResearchQueryRow).where(ResearchQueryRow.run_id == package.run_id,
                                                     ResearchQueryRow.event_id == package.event_id))
            s.execute(delete(ResearchPackageRow).where(ResearchPackageRow.run_id == package.run_id,
                                                       ResearchPackageRow.event_id == package.event_id))
            for r in results:
                s.add(ResearchResultRow(
                    run_id=package.run_id, event_id=package.event_id, result_id=r.result_id,
                    canonical_url=r.canonical_url, publisher=r.publisher, published_at=r.published_at,
                    content_type=r.content_type, relevance=r.relevance_score,
                    credibility=r.credibility_assessment.score, selected=r.selected,
                    payload=r.model_dump(mode="json")))
            for q in queries:
                s.add(ResearchQueryRow(run_id=package.run_id, event_id=package.event_id, query_id=q.query_id,
                                       query_type=q.query_type, provider=q.provider, text=q.text, status=q.status,
                                       hits=q.hits, error=(q.error or None) and q.error[:1000],
                                       payload=q.model_dump(mode="json")))
            s.add(ResearchPackageRow(run_id=package.run_id, event_id=package.event_id, status=package.status,
                                     payload=package.model_dump(mode="json"), created_at=package.created_at))

    def add_results(self, run_id: str, event_id: str, results: list[ResearchResult],
                    queries: list[QueryStatus]) -> None:
        """Append sources found later (Phase 3 viewpoint searches); selected ones join the package."""
        with self.db.session() as s:
            have = {row.result_id: row for row in s.scalars(select(ResearchResultRow).where(
                ResearchResultRow.run_id == run_id, ResearchResultRow.event_id == event_id))}
            for r in results:
                if r.result_id in have:
                    if r.selected and not have[r.result_id].selected:  # rejected earlier, chosen now
                        have[r.result_id].selected = True
                        have[r.result_id].payload = r.model_dump(mode="json")
                    continue
                s.add(ResearchResultRow(
                    run_id=run_id, event_id=event_id, result_id=r.result_id, canonical_url=r.canonical_url,
                    publisher=r.publisher, published_at=r.published_at, content_type=r.content_type,
                    relevance=r.relevance_score, credibility=r.credibility_assessment.score, selected=r.selected,
                    payload=r.model_dump(mode="json")))
            qhave = set(s.scalars(select(ResearchQueryRow.query_id).where(
                ResearchQueryRow.run_id == run_id, ResearchQueryRow.event_id == event_id)))
            for q in queries:
                if q.query_id not in qhave:
                    s.add(ResearchQueryRow(run_id=run_id, event_id=event_id, query_id=q.query_id,
                                           query_type=q.query_type, provider=q.provider, text=q.text, status=q.status,
                                           hits=q.hits, error=(q.error or None) and q.error[:1000],
                                           payload=q.model_dump(mode="json")))
            row = s.scalars(select(ResearchPackageRow).where(ResearchPackageRow.run_id == run_id,
                                                             ResearchPackageRow.event_id == event_id)).first()
            if row is not None:
                pkg = ResearchPackage.model_validate(row.payload)
                pkg.result_ids = list(dict.fromkeys(pkg.result_ids + [r.result_id for r in results if r.selected]))
                pkg.queries = pkg.queries + [q for q in queries if q.query_id not in {x.query_id for x in pkg.queries}]
                row.payload = pkg.model_dump(mode="json")

    def package(self, run_id: str, event_id: str) -> ResearchPackage | None:
        with self.db.session() as s:
            row = s.scalars(select(ResearchPackageRow).where(ResearchPackageRow.run_id == run_id,
                                                             ResearchPackageRow.event_id == event_id)).first()
            return ResearchPackage.model_validate(row.payload) if row else None

    def packages(self, run_id: str) -> dict[str, ResearchPackage]:
        with self.db.session() as s:
            rows = s.scalars(select(ResearchPackageRow).where(ResearchPackageRow.run_id == run_id)).all()
            return {r.event_id: ResearchPackage.model_validate(r.payload) for r in rows}

    def latest_package(self, event_id: str) -> ResearchPackage | None:
        """Most recent research package for an event across runs."""
        with self.db.session() as s:
            row = s.scalars(select(ResearchPackageRow).where(ResearchPackageRow.event_id == event_id)
                            .order_by(ResearchPackageRow.created_at.desc(), ResearchPackageRow.id.desc())).first()
            return ResearchPackage.model_validate(row.payload) if row else None

    def results(self, run_id: str, event_id: str, selected_only: bool = False) -> list[ResearchResult]:
        with self.db.session() as s:
            q = select(ResearchResultRow).where(ResearchResultRow.run_id == run_id,
                                                ResearchResultRow.event_id == event_id)
            if selected_only:
                q = q.where(ResearchResultRow.selected.is_(True))
            rows = s.scalars(q.order_by(ResearchResultRow.selected.desc(), ResearchResultRow.relevance.desc(),
                                        ResearchResultRow.id)).all()
            return [ResearchResult.model_validate(r.payload) for r in rows]

    def queries(self, run_id: str, event_id: str) -> list[QueryStatus]:
        with self.db.session() as s:
            rows = s.scalars(select(ResearchQueryRow).where(ResearchQueryRow.run_id == run_id,
                                                            ResearchQueryRow.event_id == event_id)
                             .order_by(ResearchQueryRow.id)).all()
            return [QueryStatus.model_validate(r.payload) for r in rows]

    def researched_event_ids(self, run_id: str) -> set[str]:
        with self.db.session() as s:
            return set(s.scalars(select(ResearchPackageRow.event_id).where(
                ResearchPackageRow.run_id == run_id, ResearchPackageRow.status.in_(("completed", "partial")))))

    def item_count(self) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(ResearchItemRow)) or 0

    def prune_old(self, keep_days: int, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.prune_items(now - timedelta(days=keep_days))
