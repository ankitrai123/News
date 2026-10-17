"""SQLite persistence (SQLAlchemy 2.0).

Phase 1 tables only; later phases add their own tables via `SCHEMA_VERSION`
migrations in `migrate()`. Every table uses stable string IDs, UTC timestamps,
uniqueness constraints and indexes on the columns used for lookups.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text,
                        UniqueConstraint, create_engine, event, select, text)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

SCHEMA_VERSION = 2  # v2: llm_calls, llm_cache, embedding_cache


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite drops tz info; store naive UTC and return aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to DB; use timezone-aware UTC")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    pass


class ArticleRow(Base):
    __tablename__ = "articles"
    article_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    normalized_title: Mapped[str] = mapped_column(Text, index=True)
    syndication_group: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(200), index=True)
    source_domain: Mapped[str | None] = mapped_column(String(200))
    source_type: Mapped[str | None] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(40))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    date_source: Mapped[str] = mapped_column(String(20))
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    language: Mapped[str | None] = mapped_column(String(10))
    country: Mapped[str | None] = mapped_column(String(80))
    category: Mapped[list] = mapped_column(JSON, default=list)
    author: Mapped[str | None] = mapped_column(String(300))
    content_access: Mapped[str] = mapped_column(String(20), default="metadata")


class EventRow(Base):
    """A real-world event, stable across runs (ongoing stories keep their ID)."""

    __tablename__ = "events"
    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(60), index=True)
    subtopic: Mapped[str | None] = mapped_column(String(120))
    countries: Mapped[list] = mapped_column(JSON, default=list)
    entities: Mapped[list] = mapped_column(JSON, default=list)
    centroid: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    first_article_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_development_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    last_run_id: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EventArticleRow(Base):
    __tablename__ = "event_articles"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True)
    article_id: Mapped[str] = mapped_column(ForeignKey("articles.article_id", ondelete="CASCADE"), primary_key=True)
    first_run_id: Mapped[str] = mapped_column(String(40))
    similarity: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (Index("ix_event_articles_article", "article_id"),)


class EventScoreRow(Base):
    """One run's assessment of an event: score, breakdown and rationale (keeps history)."""

    __tablename__ = "event_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), index=True)
    rank: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    importance: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)  # full RankedEvent
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("run_id", "event_id", name="uq_event_score_run_event"),)


class PipelineRunRow(Base):
    __tablename__ = "pipeline_runs"
    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), index=True)  # running|completed|failed
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(Text)


class StageRunRow(Base):
    """Persisted stage output so an interrupted run resumes without repeating work."""

    __tablename__ = "stage_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.run_id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20))  # running|completed|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("run_id", "stage", name="uq_stage_run"),)


class FetchLogRow(Base):
    """Every provider request: drives quota tracking, feed health and error inspection."""

    __tablename__ = "fetch_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    target: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class FeedStateRow(Base):
    """HTTP cache validators and health per feed URL."""

    __tablename__ = "feed_state"
    url: Mapped[str] = mapped_column(Text, primary_key=True)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_item_count: Mapped[int | None] = mapped_column(Integer)
    newest_item_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class LLMCallRow(Base):
    """Every model request (cache hits included): drives quota tracking and debugging."""

    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(200))
    task: Mapped[str] = mapped_column(String(60))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class LLMCacheRow(Base):
    """Response cache keyed by a hash of (provider, model, messages, parameters)."""

    __tablename__ = "llm_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(200))
    response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class EmbeddingCacheRow(Base):
    """Embedding per (model, text) as float32 bytes; pruned after a couple of weeks."""

    __tablename__ = "embedding_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(200))
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class MetaRow(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{self.path}"
        self.engine: Engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self.migrate()

    def migrate(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.session() as s:
            row = s.get(MetaRow, "schema_version")
            current = int(row.value) if row else 0
            # v2 only adds tables, which create_all() above already created.
            # Future column changes go here: `if current < 3: s.execute(text("ALTER TABLE ..."))`
            if current < SCHEMA_VERSION:
                s.merge(MetaRow(key="schema_version", value=str(SCHEMA_VERSION)))

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def schema_version(self) -> int:
        with self.session() as s:
            row = s.execute(select(MetaRow).where(MetaRow.key == "schema_version")).scalar_one_or_none()
            return int(row.value) if row else 0

    def integrity_check(self) -> str:
        with self.engine.connect() as c:
            return c.execute(text("PRAGMA integrity_check")).scalar_one()


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
