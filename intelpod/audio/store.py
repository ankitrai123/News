"""Database reads/writes for Phase 6 audio renders (one per run)."""
from __future__ import annotations

from ..db import AudioRenderRow, Database
from .models import AudioRender


class AudioStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, ar: AudioRender) -> None:
        with self.db.session() as s:
            s.merge(AudioRenderRow(run_id=ar.run_id, status=ar.status, payload=ar.model_dump(mode="json"),
                                   created_at=ar.created_at))

    def render(self, run_id: str) -> AudioRender | None:
        with self.db.session() as s:
            row = s.get(AudioRenderRow, run_id)
            return AudioRender.model_validate(row.payload) if row else None
