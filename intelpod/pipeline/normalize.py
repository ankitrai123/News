"""RawItem -> Article: canonical identity, clean title, validated dates."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..schemas import Article, RawItem
from ..textutil import (canonicalize_url, domain_of, looks_like_table, normalize_title, stable_hash, strip_html,
                        strip_publisher_suffix)

log = logging.getLogger(__name__)

MIN_VALID_DATE = datetime(2000, 1, 1, tzinfo=timezone.utc)
FUTURE_TOLERANCE = timedelta(hours=2)


def validate_published_at(value: datetime | None, now: datetime) -> tuple[datetime | None, bool]:
    """Return (date, valid). Naive dates are assumed UTC; future or ancient dates are rejected."""
    if value is None:
        return None, True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    if value > now + FUTURE_TOLERANCE or value < MIN_VALID_DATE:
        return None, False
    return value, True


def normalize_item(item: RawItem, now: datetime | None = None, max_description_chars: int = 400) -> Article | None:
    now = now or datetime.now(timezone.utc)
    title_raw = strip_html(item.title)
    if not title_raw or not item.url:
        return None
    try:
        canonical = canonicalize_url(item.url)
    except ValueError:
        log.debug("unparseable URL skipped: %r", item.url)
        return None
    domain = item.source_domain or domain_of(item.url)
    source_name = (item.source_name or domain or "unknown").strip()
    title = strip_publisher_suffix(title_raw, source_name, domain)
    norm = normalize_title(title)
    if len(norm) < 8:  # "Live", "Watch:" etc. are not articles
        return None

    published, valid = validate_published_at(item.published_at, now)
    date_source = item.date_source if published else ("invalid" if not valid else "missing")

    desc = strip_html(item.description or "") or None
    if desc and normalize_title(desc) == norm:
        desc = None  # many feeds repeat the title as the description
    if desc and looks_like_table(desc):
        desc = None  # a flattened table of figures is not a summary (and confuses topic/country tagging)
    if desc and len(desc) > max_description_chars:
        desc = desc[:max_description_chars].rsplit(" ", 1)[0] + "…"

    lang = (item.language or "en").lower()[:5]
    return Article(
        article_id=stable_hash(canonical),
        title=title,
        description=desc,
        source_name=source_name,
        source_domain=domain,
        source_url=item.url,
        canonical_url=canonical,
        published_at=published,
        date_source=date_source,
        discovered_at=now,
        language=lang,
        country=item.country,
        category=sorted(set(item.categories)),
        author=(item.author or None),
        content_access="metadata",
        provider=item.provider,
        normalized_title=norm,
        syndication_group=stable_hash(norm),
        source_type=item.source_type,
    )
