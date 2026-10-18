"""Research feeds: official bodies, international organisations and think tanks.

Each feed is fetched at most once per run (conditional GET, robots.txt, per-host spacing via
the shared HttpClient), its items are stored in `research_items` and then matched to events
locally. Feed items never enter news clustering. A feed that keeps failing is skipped for a day,
like news feeds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import HttpClient
from ..providers.rss import FeedSpec, RssProvider, load_feeds
from ..textutil import canonicalize_url, domain_of, stable_hash, strip_html

log = logging.getLogger(__name__)

PROVIDER = "research_feeds"


class ResearchFeedProvider(RssProvider):
    name = PROVIDER


def load_research_feeds(path: Path) -> list[FeedSpec]:
    return load_feeds(path) if path.exists() else []


def active_feeds(feeds: list[FeedSpec], states: dict, pause_after: int, retry_hours: float,
                 now: datetime | None = None) -> tuple[list[FeedSpec], list[str]]:
    """Skip feeds that keep failing or that robots.txt forbids; retry each paused one daily."""
    now = now or datetime.now(timezone.utc)
    active, paused = [], []
    for f in feeds:
        st = states.get(f.url)
        failing = st is not None and (st.consecutive_failures or 0) >= pause_after
        robots = st is not None and "robots.txt" in (st.last_error or "")
        due = st is None or st.last_checked_at is None or now - st.last_checked_at >= timedelta(hours=retry_hours)
        if (failing or robots) and not due:
            paused.append(f.name)
        else:
            active.append(f)
    return active, paused


def refresh_research_feeds(http: HttpClient, feeds: list[FeedSpec], repo, store, run_id: str | None,
                           max_description_chars: int = 400, workers: int = 4) -> dict:
    """Fetch research feeds and store new items. Returns stats for the stage output."""
    if not feeds:
        return {"feeds": 0, "requests": 0, "ok": 0, "new_items": 0, "errors": []}
    provider = ResearchFeedProvider(http, feeds, validators=repo.feed_validators,
                                    max_description_chars=max_description_chars, max_workers=workers)
    res = provider.fetch()
    repo.log_fetches(run_id, res.fetches)
    now = datetime.now(timezone.utc)
    rows = []
    for it in res.items:
        try:
            canon = canonicalize_url(it.url)
        except ValueError:
            continue
        published = it.published_at if it.published_at and it.published_at <= now + timedelta(hours=2) else None
        rows.append({
            "item_id": stable_hash(canon), "canonical_url": canon, "url": it.url, "title": strip_html(it.title)[:500],
            "description": it.description, "publisher": it.source_name or domain_of(it.url) or "unknown",
            "domain": it.source_domain or domain_of(it.url), "source_type": it.source_type, "country": it.country,
            "feed_name": it.feed_name, "published_at": published, "date_source": "feed" if published else "missing",
            "discovered_at": now,
        })
    new = store.save_items(rows)
    return {"feeds": len(feeds), "requests": len(res.fetches), "ok": sum(f.ok for f in res.fetches),
            "items": len(rows), "new_items": new,
            "errors": [f"{f.target}: {f.error}" for f in res.fetches if not f.ok][:20]}
