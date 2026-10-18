"""Database reads/writes for Phase 4: impact analyses, event links, forecast ledger, briefs."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select

from ..db import (Database, EventLinkRow, EventRow, ForecastRow, ImpactAnalysisRow, IntelligenceBriefRow)
from .models import EventLink, Forecast, ImpactAnalysis, IntelligenceBrief


class ImpactStore:
    def __init__(self, db: Database):
        self.db = db

    # -- impact analyses ------------------------------------------------------------------
    def save(self, a: ImpactAnalysis) -> None:
        with self.db.session() as s:
            s.execute(delete(ImpactAnalysisRow).where(ImpactAnalysisRow.run_id == a.run_id,
                                                      ImpactAnalysisRow.event_id == a.event_id))
            s.add(ImpactAnalysisRow(run_id=a.run_id, event_id=a.event_id, status=a.status,
                                    payload=a.model_dump(mode="json"), created_at=a.created_at))

    def analysis(self, run_id: str, event_id: str) -> ImpactAnalysis | None:
        with self.db.session() as s:
            row = s.scalars(select(ImpactAnalysisRow).where(ImpactAnalysisRow.run_id == run_id,
                                                            ImpactAnalysisRow.event_id == event_id)).first()
            return ImpactAnalysis.model_validate(row.payload) if row else None

    def analyses(self, run_id: str) -> dict[str, ImpactAnalysis]:
        with self.db.session() as s:
            rows = s.scalars(select(ImpactAnalysisRow).where(ImpactAnalysisRow.run_id == run_id)).all()
            return {r.event_id: ImpactAnalysis.model_validate(r.payload) for r in rows}

    def latest_analysis(self, event_id: str) -> ImpactAnalysis | None:
        with self.db.session() as s:
            row = s.scalars(select(ImpactAnalysisRow).where(ImpactAnalysisRow.event_id == event_id)
                            .order_by(ImpactAnalysisRow.created_at.desc(), ImpactAnalysisRow.id.desc())).first()
            return ImpactAnalysis.model_validate(row.payload) if row else None

    def previous_analysis(self, event_id: str, run_id: str) -> ImpactAnalysis | None:
        """The most recent analysis of this event from another (earlier) run."""
        with self.db.session() as s:
            row = s.scalars(select(ImpactAnalysisRow).where(ImpactAnalysisRow.event_id == event_id,
                                                            ImpactAnalysisRow.run_id != run_id,
                                                            ImpactAnalysisRow.status.in_(("completed", "partial")))
                            .order_by(ImpactAnalysisRow.created_at.desc(), ImpactAnalysisRow.id.desc())).first()
            return ImpactAnalysis.model_validate(row.payload) if row else None

    def done_event_ids(self, run_id: str) -> set[str]:
        with self.db.session() as s:
            return set(s.scalars(select(ImpactAnalysisRow.event_id).where(
                ImpactAnalysisRow.run_id == run_id, ImpactAnalysisRow.status.in_(("completed", "partial")))))

    # -- earlier events -----------------------------------------------------------------------
    def earlier_events(self, event_id: str, before: datetime, since: datetime) -> list[EventRow]:
        """Events IntelPod saw before this one started, within the look-back window."""
        with self.db.session() as s:
            return list(s.scalars(select(EventRow).where(
                EventRow.event_id != event_id, EventRow.first_seen_at >= since,
                EventRow.first_seen_at <= before)).all())

    # -- links --------------------------------------------------------------------------------
    def save_links(self, run_id: str, links: list[EventLink]) -> None:
        with self.db.session() as s:
            s.execute(delete(EventLinkRow).where(EventLinkRow.run_id == run_id))
            for link in links:
                s.add(EventLinkRow(run_id=run_id, link_id=link.link_id, event_a=link.event_a, event_b=link.event_b,
                                   kind=link.kind, payload=link.model_dump(mode="json")))

    def links(self, run_id: str) -> list[EventLink]:
        with self.db.session() as s:
            rows = s.scalars(select(EventLinkRow).where(EventLinkRow.run_id == run_id).order_by(EventLinkRow.id)).all()
            return [EventLink.model_validate(r.payload) for r in rows]

    # -- forecast ledger ------------------------------------------------------------------------
    def upsert_forecasts(self, items: list[Forecast]) -> list[str]:
        """Add new forecasts; for known ones record that this run saw them again. Returns new ids.
        Outcomes already recorded are never overwritten."""
        new = []
        with self.db.session() as s:
            for f in items:
                row = s.get(ForecastRow, f.forecast_id)
                if row is None:
                    s.add(ForecastRow(forecast_id=f.forecast_id, event_id=f.event_id, status=f.status,
                                      payload=f.model_dump(mode="json"), first_seen_at=f.first_seen_at))
                    new.append(f.forecast_id)
                    continue
                old = Forecast.model_validate(row.payload)
                old.last_run_id = f.last_run_id
                old.source_ids = list(dict.fromkeys(old.source_ids + f.source_ids))
                row.payload = old.model_dump(mode="json")
                row.updated_at = datetime.now(timezone.utc)
        return new

    def forecasts(self, status: str | None = None, event_id: str | None = None, limit: int = 500) -> list[Forecast]:
        with self.db.session() as s:
            q = select(ForecastRow)
            if status:
                q = q.where(ForecastRow.status == status)
            if event_id:
                q = q.where(ForecastRow.event_id == event_id)
            rows = s.scalars(q.order_by(ForecastRow.first_seen_at.desc()).limit(limit)).all()
            return [Forecast.model_validate(r.payload) for r in rows]

    def resolve_forecast(self, forecast_id: str, verdict: str, outcome: str) -> Forecast | None:
        with self.db.session() as s:
            row = s.get(ForecastRow, forecast_id)
            if row is None:
                return None
            f = Forecast.model_validate(row.payload)
            f.status, f.verdict, f.outcome = "resolved", verdict, outcome.strip()[:1000]
            f.evaluated_at = datetime.now(timezone.utc)
            row.status, row.payload, row.updated_at = f.status, f.model_dump(mode="json"), f.evaluated_at
            return f

    # -- brief ----------------------------------------------------------------------------------
    def save_brief(self, b: IntelligenceBrief) -> None:
        with self.db.session() as s:
            s.merge(IntelligenceBriefRow(run_id=b.run_id, payload=b.model_dump(mode="json"), created_at=b.created_at))

    def brief(self, run_id: str) -> IntelligenceBrief | None:
        with self.db.session() as s:
            row = s.get(IntelligenceBriefRow, run_id)
            return IntelligenceBrief.model_validate(row.payload) if row else None
