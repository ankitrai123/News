"""Provider-independent news ingestion interface.

A provider turns some external source into `RawItem`s and reports every request it
made as a `FetchRecord`. Providers never raise for a single failing feed/query:
they record the failure and continue, so one broken source cannot sink a run.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime

from ..schemas import RawItem


@dataclass
class FetchRecord:
    provider: str
    target: str
    started_at: datetime
    duration_ms: int
    ok: bool
    status_code: int | None = None
    item_count: int = 0
    error: str | None = None
    # Feed-specific cache validators / health, written back to feed_state.
    etag: str | None = None
    last_modified: str | None = None
    newest_item_at: datetime | None = None
    not_modified: bool = False


@dataclass
class ProviderResult:
    items: list[RawItem] = field(default_factory=list)
    fetches: list[FetchRecord] = field(default_factory=list)


class NewsProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def fetch(self) -> ProviderResult:
        """Fetch recent items. Must not raise for individual source failures."""
