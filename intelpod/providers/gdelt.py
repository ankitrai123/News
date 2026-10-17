"""GDELT DOC 2.0 API provider (free, no key).

Endpoint: https://api.gdeltproject.org/api/v2/doc/doc  (mode=ArtList, format=json)
Notes / limitations (documented in docs/PROVIDERS.md):
  * Returns metadata only (url, title, seendate, domain, language, sourcecountry),
    no article text or description.
  * `seendate` is when GDELT first saw the URL, not the publisher's timestamp.
  * ArtList searches cover a rolling ~3 month window; max 250 records per request.
  * GDELT asks clients to throttle (about one request every 5 s); when exceeded it
    answers with a plain-text notice instead of JSON, which we treat as an error.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from ..config import GdeltProviderConfig
from ..http import FetchError, HttpClient
from ..schemas import RawItem
from .base import FetchRecord, NewsProvider, ProviderResult

log = logging.getLogger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_HOST = "api.gdeltproject.org"

_LANG = {"english": "en", "hindi": "hi", "french": "fr", "german": "de", "spanish": "es", "chinese": "zh",
         "arabic": "ar", "russian": "ru", "japanese": "ja", "portuguese": "pt"}


class GdeltError(Exception):
    pass


def parse_seendate(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_artlist(body: str) -> list[dict]:
    """Parse an ArtList JSON body. Raises GdeltError on non-JSON (e.g. rate-limit notices)."""
    text = body.strip()
    if not text:
        return []  # GDELT returns an empty body when nothing matches
    if not text.startswith("{"):
        raise GdeltError(f"non-JSON response: {text[:160]!r}")
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError as e:
        raise GdeltError(f"malformed JSON: {e}") from e
    articles = data.get("articles", []) if isinstance(data, dict) else []
    return [a for a in articles if isinstance(a, dict)]


class GdeltProvider(NewsProvider):
    name = "gdelt"

    def __init__(self, http: HttpClient, cfg: GdeltProviderConfig, domain_publishers: dict[str, str] | None = None):
        self.http = http
        self.cfg = cfg
        self.domain_publishers = domain_publishers or {}
        http.set_host_interval(GDELT_HOST, cfg.min_interval_seconds)

    def build_params(self, query: str) -> dict[str, str]:
        q = f"{query} {self.cfg.language_filter}".strip()
        return {"query": q, "mode": "ArtList", "format": "json", "maxrecords": str(self.cfg.max_records),
                "timespan": self.cfg.timespan, "sort": self.cfg.sort}

    def fetch(self) -> ProviderResult:
        result = ProviderResult()
        for q in self.cfg.queries:
            items, record = self.fetch_query(q.query, q.topic)
            result.items.extend(items)
            result.fetches.append(record)
        return result

    def fetch_query(self, query: str, topic: str | None = None) -> tuple[list[RawItem], FetchRecord]:
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        params = self.build_params(query)

        def record(**kw) -> FetchRecord:
            return FetchRecord(provider=self.name, target=f"query={params['query']}", started_at=started,
                               duration_ms=int((time.monotonic() - t0) * 1000), **kw)

        try:
            # GDELT's API is a documented public endpoint; its robots.txt targets crawlers of the site.
            resp = self.http.get(GDELT_DOC_URL, params=params, check_robots=False)
        except FetchError as e:
            return [], record(ok=False, status_code=e.status_code, error=str(e))
        if resp.status_code >= 400:
            return [], record(ok=False, status_code=resp.status_code, error=f"HTTP {resp.status_code}")
        try:
            rows = parse_artlist(resp.text)
        except GdeltError as e:
            log.warning("GDELT query %r: %s", query, e)
            return [], record(ok=False, status_code=resp.status_code, error=str(e))
        items = [it for row in rows if (it := self._to_item(row, topic)) is not None]
        newest = max((i.published_at for i in items if i.published_at), default=None)
        return items, record(ok=True, status_code=resp.status_code, item_count=len(items), newest_item_at=newest)

    def _to_item(self, row: dict, topic: str | None) -> RawItem | None:
        url, title = (row.get("url") or "").strip(), (row.get("title") or "").strip()
        if not url.startswith(("http://", "https://")) or not title:
            return None
        domain = (row.get("domain") or "").lower().removeprefix("www.") or None
        seen = parse_seendate(row.get("seendate"))
        lang = (row.get("language") or "").strip().lower()
        return RawItem(
            provider=self.name, title=title, url=url, description=None,
            source_name=self._publisher_for(domain), source_domain=domain,
            published_at=seen, date_source="gdelt_seendate" if seen else "missing",
            language=_LANG.get(lang, lang[:2] or None), country=(row.get("sourcecountry") or None),
            categories=[topic] if topic else [], source_type="news",
        )

    def _publisher_for(self, domain: str | None) -> str | None:
        if not domain:
            return None
        for known, publisher in self.domain_publishers.items():
            if domain == known or domain.endswith("." + known):
                return publisher
        return domain
