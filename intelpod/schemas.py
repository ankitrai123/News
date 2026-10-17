"""Typed data contracts passed between pipeline stages."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DateSource = Literal["feed", "gdelt_seendate", "missing", "invalid"]


class RawItem(BaseModel):
    """What a provider returns before normalization. Every field but url/title may be absent."""

    provider: str
    title: str
    url: str
    description: str | None = None
    source_name: str | None = None
    source_domain: str | None = None
    published_at: datetime | None = None
    date_source: DateSource = "missing"
    language: str | None = None
    country: str | None = None
    categories: list[str] = Field(default_factory=list)
    author: str | None = None
    feed_name: str | None = None
    source_type: str | None = None


class Article(BaseModel):
    article_id: str
    title: str
    description: str | None = None
    source_name: str
    source_domain: str | None = None
    source_url: str
    canonical_url: str
    published_at: datetime | None = None
    date_source: DateSource = "missing"
    discovered_at: datetime
    language: str | None = "en"
    country: str | None = None
    category: list[str] = Field(default_factory=list)
    author: str | None = None
    content_access: Literal["metadata", "authorized_text"] = "metadata"
    provider: str
    normalized_title: str
    syndication_group: str
    source_type: str | None = None

    @property
    def effective_time(self) -> datetime:
        return self.published_at or self.discovered_at


class ScoreComponent(BaseModel):
    name: str
    value: float = Field(ge=0, le=1)
    weight: float
    evidence: list[str] = Field(default_factory=list)


class EventCandidate(BaseModel):
    """Output of clustering for one event in one run."""

    event_id: str
    title: str
    description: str | None = None
    article_ids: list[str]
    representative_article_id: str
    topic: str
    subtopic: str | None = None
    topics: dict[str, int] = Field(default_factory=dict)
    countries: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)
    independent_sources: int = 0
    first_article_at: datetime | None = None
    latest_article_at: datetime | None = None
    cluster_confidence: float | None = None
    is_continuing: bool = False
    previous_first_seen_at: datetime | None = None
    new_article_count: int = 0
    centroid: dict[str, float] = Field(default_factory=dict)


class RankedEvent(BaseModel):
    rank: int | None
    selected: bool
    event_id: str
    title: str
    description: str | None
    topic: str
    subtopic: str | None
    countries: list[str]
    publishers: list[str]
    independent_sources: int
    article_count: int
    importance: float = Field(ge=0, le=100)
    components: list[ScoreComponent]
    penalties: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    rationale: str
    first_article_at: datetime | None
    latest_article_at: datetime | None
    cluster_confidence: float | None
    is_continuing: bool
