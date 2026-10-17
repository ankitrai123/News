"""RSS/Atom provider using publicly published feeds.

Uses conditional GET (ETag / Last-Modified) so unchanged feeds cost one cheap 304.
Only metadata and the feed's own short summary are kept.
"""
from __future__ import annotations

import calendar
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import feedparser
import yaml

from ..http import FetchError, HttpClient
from ..schemas import RawItem
from ..textutil import strip_html
from .base import FetchRecord, NewsProvider, ProviderResult

log = logging.getLogger(__name__)


@dataclass
class FeedSpec:
    name: str
    url: str
    publisher: str
    domain: str | None = None
    country: str | None = None
    region: str | None = None
    source_type: str = "news"
    categories: tuple[str, ...] = ()
    enabled: bool = True
    verified: bool = False
    language: str = "en"


def load_feeds(path: Path, include_disabled: bool = False) -> list[FeedSpec]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feeds = []
    for entry in data.get("feeds", []):
        spec = FeedSpec(
            name=entry["name"], url=entry["url"], publisher=entry.get("publisher") or entry["name"],
            domain=entry.get("domain"), country=entry.get("country"), region=entry.get("region"),
            source_type=entry.get("source_type", "news"), categories=tuple(entry.get("categories") or ()),
            enabled=bool(entry.get("enabled", True)), verified=bool(entry.get("verified", False)),
            language=entry.get("language", "en"),
        )
        if spec.enabled or include_disabled:
            feeds.append(spec)
    return feeds


ValidatorLookup = Callable[[str], tuple[str | None, str | None]]


class RssProvider(NewsProvider):
    name = "rss"

    def __init__(self, http: HttpClient, feeds: list[FeedSpec], validators: ValidatorLookup | None = None,
                 max_description_chars: int = 400, use_conditional_get: bool = True):
        self.http = http
        self.feeds = feeds
        self.validators = validators or (lambda url: (None, None))
        self.max_desc = max_description_chars
        self.use_conditional_get = use_conditional_get

    def fetch(self) -> ProviderResult:
        result = ProviderResult()
        for feed in self.feeds:
            items, record = self.fetch_feed(feed)
            result.items.extend(items)
            result.fetches.append(record)
        return result

    def fetch_feed(self, feed: FeedSpec) -> tuple[list[RawItem], FetchRecord]:
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"}
        if self.use_conditional_get:
            etag, last_modified = self.validators(feed.url)
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        def record(**kw) -> FetchRecord:
            return FetchRecord(provider=self.name, target=feed.url, started_at=started,
                               duration_ms=int((time.monotonic() - t0) * 1000), **kw)

        try:
            resp = self.http.get(feed.url, headers=headers)
        except FetchError as e:
            log.warning("feed %s failed: %s", feed.name, e)
            return [], record(ok=False, status_code=e.status_code, error=str(e))
        if resp.status_code == 304:
            return [], record(ok=True, status_code=304, not_modified=True)
        if resp.status_code >= 400:
            return [], record(ok=False, status_code=resp.status_code, error=f"HTTP {resp.status_code}")

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            err = f"unparseable feed: {getattr(parsed, 'bozo_exception', 'unknown error')}"
            log.warning("feed %s: %s", feed.name, err)
            return [], record(ok=False, status_code=resp.status_code, error=err[:500])

        items = [it for e in parsed.entries if (it := self._to_item(e, feed)) is not None]
        newest = max((i.published_at for i in items if i.published_at), default=None)
        return items, record(ok=True, status_code=resp.status_code, item_count=len(items),
                             etag=resp.headers.get("ETag"), last_modified=resp.headers.get("Last-Modified"),
                             newest_item_at=newest)

    def _to_item(self, entry, feed: FeedSpec) -> RawItem | None:
        title = strip_html(entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith(("http://", "https://")):
            return None
        published, date_source = None, "missing"
        for key in ("published_parsed", "updated_parsed"):
            st = entry.get(key)
            if st:
                try:
                    published = datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
                    date_source = "feed"
                    break
                except (OverflowError, ValueError, TypeError):
                    continue
        desc = strip_html(entry.get("summary") or entry.get("description") or "")
        if len(desc) > self.max_desc:
            desc = desc[: self.max_desc].rsplit(" ", 1)[0] + "…"
        tags = [t.get("term") for t in entry.get("tags", []) if isinstance(t, dict) and t.get("term")]
        return RawItem(
            provider=self.name, title=title, url=link, description=desc or None,
            source_name=feed.publisher, source_domain=feed.domain, published_at=published,
            date_source=date_source, language=feed.language, country=feed.country,
            categories=list(feed.categories) + [str(t)[:60] for t in tags[:5]],
            author=(entry.get("author") or None), feed_name=feed.name, source_type=feed.source_type,
        )
