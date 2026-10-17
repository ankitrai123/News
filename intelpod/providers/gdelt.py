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
from ..http import FetchError, HttpClient, _retry_after_seconds as _retry_after
from ..textutil import repair_gdelt_title
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


RATE_LIMIT_HINTS = ("limit requests", "too many requests", "rate limit")


class GdeltProvider(NewsProvider):
    """Queries are sent one at a time, `min_interval_seconds` apart. When GDELT refuses a
    request (HTTP 429 or its plain-text "Please limit requests" notice), the provider waits
    `cooldown_seconds` (or Retry-After), doubling after each refusal, and retries that query
    once. After `stop_after_refusals` queries are refused even after a cool-down, GDELT is
    skipped for the rest of the run instead of burning minutes on retries."""

    name = "gdelt"

    def __init__(self, http: HttpClient, cfg: GdeltProviderConfig, domain_publishers: dict[str, str] | None = None,
                 sleep=None):
        self.http = http
        self.cfg = cfg
        self.domain_publishers = domain_publishers or {}
        self._sleep = sleep or http._sleep
        self.skipped_reason: str | None = None
        http.set_host_interval(GDELT_HOST, cfg.min_interval_seconds)

    def build_params(self, query: str) -> dict[str, str]:
        q = f"{query} {self.cfg.language_filter}".strip()
        return {"query": q, "mode": "ArtList", "format": "json", "maxrecords": str(self.cfg.max_records),
                "timespan": self.cfg.timespan, "sort": self.cfg.sort}

    def fetch(self) -> ProviderResult:
        result = ProviderResult()
        cooldown = self.cfg.cooldown_seconds
        refused = 0
        for i, q in enumerate(self.cfg.queries):
            if refused >= self.cfg.stop_after_refusals:
                left = len(self.cfg.queries) - i
                self.skipped_reason = (f"GDELT kept refusing requests (rate limit); skipped {left} remaining "
                                       f"quer{'y' if left == 1 else 'ies'} this run")
                log.warning(self.skipped_reason)
                break
            items, record, wait = self._fetch(q.query, q.topic)
            if wait is not None:
                result.fetches.append(record)
                pause = min(max(wait, cooldown), self.cfg.max_cooldown_seconds)
                log.info("GDELT refused a request; cooling down %.0f s before retrying", pause)
                self._sleep(pause)
                cooldown = min(cooldown * 2, self.cfg.max_cooldown_seconds)
                items, record, wait = self._fetch(q.query, q.topic)
                if wait is not None:
                    refused += 1
            result.items.extend(items)
            result.fetches.append(record)
        return result

    def fetch_query(self, query: str, topic: str | None = None) -> tuple[list[RawItem], FetchRecord]:
        items, record, _ = self._fetch(query, topic)
        return items, record

    def _fetch(self, query: str, topic: str | None) -> tuple[list[RawItem], FetchRecord, float | None]:
        """Returns (items, record, wait) where wait is not None when GDELT refused (rate limit)."""
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        params = self.build_params(query)

        def record(**kw) -> FetchRecord:
            return FetchRecord(provider=self.name, target=f"query={params['query']}", started_at=started,
                               duration_ms=int((time.monotonic() - t0) * 1000), **kw)

        try:
            # Documented public API (robots.txt targets site crawlers). 429 is handled here, not
            # by blind HTTP retries, which only prolong GDELT's refusal window.
            resp = self.http.request("GET", GDELT_DOC_URL, params=params, check_robots=False,
                                     retry_statuses={500, 502, 503, 504})
        except FetchError as e:
            return [], record(ok=False, status_code=e.status_code, error=str(e)), None
        if resp.status_code == 429:
            wait = _retry_after(resp) or 0.0
            return [], record(ok=False, status_code=429, error="HTTP 429: rate limited by GDELT"), wait
        if resp.status_code >= 400:
            return [], record(ok=False, status_code=resp.status_code, error=f"HTTP {resp.status_code}"), None
        try:
            rows = parse_artlist(resp.text)
        except GdeltError as e:
            limited = any(h in str(e).lower() for h in RATE_LIMIT_HINTS)
            log.warning("GDELT query %r: %s", query, e)
            return [], record(ok=False, status_code=resp.status_code, error=str(e)), (0.0 if limited else None)
        items = [it for row in rows if (it := self._to_item(row, topic)) is not None]
        newest = max((i.published_at for i in items if i.published_at), default=None)
        return items, record(ok=True, status_code=resp.status_code, item_count=len(items), newest_item_at=newest), None

    def _to_item(self, row: dict, topic: str | None) -> RawItem | None:
        url, title = (row.get("url") or "").strip(), repair_gdelt_title(row.get("title") or "")
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
