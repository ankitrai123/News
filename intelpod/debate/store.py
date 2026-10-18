"""Database reads/writes for Phase 3 debate packages and claims."""
from __future__ import annotations

from sqlalchemy import delete, select

from ..db import ClaimRow, Database, DebatePackageRow
from .models import DebatePackage


class DebateStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, pkg: DebatePackage) -> None:
        """Replace this run's debate package and claims for one event (idempotent on resume)."""
        with self.db.session() as s:
            s.execute(delete(ClaimRow).where(ClaimRow.run_id == pkg.run_id, ClaimRow.event_id == pkg.event_id))
            s.execute(delete(DebatePackageRow).where(DebatePackageRow.run_id == pkg.run_id,
                                                     DebatePackageRow.event_id == pkg.event_id))
            for c in pkg.claims:
                s.add(ClaimRow(run_id=pkg.run_id, event_id=pkg.event_id, claim_id=c.claim_id, kind=c.kind,
                               confidence=c.confidence, text=c.text, payload=c.model_dump(mode="json")))
            s.add(DebatePackageRow(run_id=pkg.run_id, event_id=pkg.event_id, status=pkg.status,
                                   payload=pkg.model_dump(mode="json"), created_at=pkg.created_at))

    def package(self, run_id: str, event_id: str) -> DebatePackage | None:
        with self.db.session() as s:
            row = s.scalars(select(DebatePackageRow).where(DebatePackageRow.run_id == run_id,
                                                           DebatePackageRow.event_id == event_id)).first()
            return DebatePackage.model_validate(row.payload) if row else None

    def packages(self, run_id: str) -> dict[str, DebatePackage]:
        with self.db.session() as s:
            rows = s.scalars(select(DebatePackageRow).where(DebatePackageRow.run_id == run_id)).all()
            return {r.event_id: DebatePackage.model_validate(r.payload) for r in rows}

    def latest_package(self, event_id: str) -> DebatePackage | None:
        with self.db.session() as s:
            row = s.scalars(select(DebatePackageRow).where(DebatePackageRow.event_id == event_id)
                            .order_by(DebatePackageRow.created_at.desc(), DebatePackageRow.id.desc())).first()
            return DebatePackage.model_validate(row.payload) if row else None

    def done_event_ids(self, run_id: str) -> set[str]:
        with self.db.session() as s:
            return set(s.scalars(select(DebatePackageRow.event_id).where(
                DebatePackageRow.run_id == run_id, DebatePackageRow.status.in_(("completed", "partial")))))
