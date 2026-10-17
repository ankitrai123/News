"""RSS/Atom provider using publicly published feeds.

Uses conditional GET (ETag / Last-Modified) so unchanged feeds cost one cheap 304.
Only metadata and the feed's own short summary are kept.
"""
from __future__ import annotations

import calendar
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
import yaml
from dateutil import parser as dateparser

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
    timezone: str | None = None   # IANA zone for feed dates without one (default: by country)


_TZ_BY_COUNTRY = {"IN": "Asia/Kolkata", "JP": "Asia/Tokyo", "HK": "Asia/Hong_Kong", "SG": "Asia/Singapore",
                  "CN": "Asia/Shanghai", "GB": "Europe/London", "DE": "Europe/Berlin", "FR": "Europe/Paris",
                  "QA": "Asia/Qatar", "AE": "Asia/Dubai", "US": "America/New_York", "EU": "Europe/Brussels",
                  "AU": "Australia/Sydney", "CA": "America/Toronto"}
_TZINFOS = {"IST": 19800, "JST": 32400, "HKT": 28800, "SGT": 28800, "BST": 3600, "CET": 3600, "CEST": 7200,
            "EST": -18000, "EDT": -14400, "CST": -21600, "CDT": -18000, "PST": -28800, "PDT": -25200,
            "GMT": 0, "UTC": 0, "Z": 0}


def feed_timezone(feed: "FeedSpec") -> str | None:
    return feed.timezone or _TZ_BY_COUNTRY.get((feed.country or "").upper())


def parse_loose_date(value, tz_name: str | None = None) -> datetime | None:
    """Parse a free-form date string; naive results are read in `tz_name` (else UTC)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = dateparser.parse(value.strip(), tzinfos=_TZINFOS, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name)) if tz_name else dt.replace(tzinfo=timezone.utc)
        except (ZoneInfoNotFoundError, ValueError):
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_feeds(path: Path, include_disabled: bool = False) -> list[FeedSpec]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feeds = []
    for entry in data.get("feeds", []):
        spec = FeedSpec(
            name=entry["name"], url=entry["url"], publisher=entry.get("publisher") or entry["name"],
            domain=entry.get("domain"), country=entry.get("country"), region=entry.get("region"),
            source_type=entry.get("source_type", "news"), categories=tuple(entry.get("categories") or ()),
            enabled=bool(entry.get("enabled", True)), verified=bool(entry.get("verified", False)),
            language=entry.get("language", "en"), timezone=entry.get("timezone"),
        )
        if spec.enabled or include_disabled:
            feeds.append(spec)
    return feeds


ValidatorLookup = Callable[[str], tuple[str | None, str | None]]


class RssProvider(NewsProvider):
    name = "rss"

    def __init__(self, http: HttpClient, feeds: list[FeedSpec], validators: ValidatorLookup | None = None,
                 max_description_chars: int = 400, use_conditional_get: bool = True, max_workers: int = 1):
        self.http = http
        self.feeds = feeds
        self.validators = validators or (lambda url: (None, None))
        self.max_desc = max_description_chars
        self.use_conditional_get = use_conditional_get
        self.max_workers = max_workers

    def fetch(self) -> ProviderResult:
        """Feeds live on different hosts, so they are fetched in parallel (per-host spacing
        and robots.txt checks still apply). Results keep the configured feed order."""
        result = ProviderResult()
        if self.max_workers <= 1 or len(self.feeds) <= 1:
            outcomes = [self.fetch_feed(f) for f in self.feeds]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="rss") as pool:
                outcomes = list(pool.map(self.fetch_feed, self.feeds))
        for items, record in outcomes:
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
        present = set(entry.keys())  # avoid feedparser's deprecated updated->published fallback
        for key in ("published_parsed", "updated_parsed"):
            st = entry.get(key) if key in present else None
            if st:
                try:
                    published = datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
                    date_source = "feed"
                    break
                except (OverflowError, ValueError, TypeError):
                    continue
        if published is None:
            # Non-standard date strings (e.g. "17 Oct 2026 18:30 IST", "October 17, 2026") that
            # feedparser cannot read: parse them flexibly, in the feed's local time if no zone.
            for key in ("published", "updated", "dc_date", "date", "created", "pubdate", "issued"):
                published = parse_loose_date(entry.get(key), feed_timezone(feed))
                if published is not None:
                    date_source = "feed"
                    break
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
