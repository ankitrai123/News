"""Database reads/writes for Phase 5 episodes (one per run; a re-run replaces it)."""
from __future__ import annotations

from sqlalchemy import select

from ..db import Database, EpisodeRow
from .models import Episode


class EpisodeStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, ep: Episode) -> None:
        with self.db.session() as s:
            s.merge(EpisodeRow(run_id=ep.run_id, episode_id=ep.episode_id, status=ep.status,
                               payload=ep.model_dump(mode="json"), created_at=ep.created_at))

    def episode(self, run_id: str) -> Episode | None:
        with self.db.session() as s:
            row = s.get(EpisodeRow, run_id)
            return Episode.model_validate(row.payload) if row else None

    def episodes(self, limit: int = 30) -> list[Episode]:
        with self.db.session() as s:
            rows = s.scalars(select(EpisodeRow).order_by(EpisodeRow.created_at.desc()).limit(limit)).all()
            return [Episode.model_validate(r.payload) for r in rows]
