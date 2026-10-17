"""NewsData.io provider (free tier: registration + API key, quota-limited).

API facts this module relies on (from NewsData's OpenAPI spec and official SDKs,
see docs/PROVIDERS.md; re-check if NewsData changes its plans):
  * GET https://newsdata.io/api/1/latest  — news from the past 48 h, 1 credit per call/page.
  * Key sent in the `X-ACCESS-KEY` header (keeps it out of URLs and logs).
  * Free plan: 200 credits/day, 30 credits per 15 min, `size` <= 10, `q` <= 100 chars,
    articles delayed ~12 h, no `timeframe`, archive, sentiment or full content.
  * Paid-only fields still appear, holding notices such as "ONLY AVAILABLE IN PAID PLANS".
  * Success: {"status": "success", "totalResults", "results": [...], "nextPage"}.
    Error:   {"status": "error", "results": {"message", "code"}} (results may be a list).
  * pubDate: "YYYY-MM-DD HH:MM:SS" in pubDateTZ (UTC by default).
  * 429 means either the 15-minute rate limit (RateLimitExceeded) or an exhausted
    daily quota (ApiLimitExceeded / ApiKeyLimitExceeded): both stop NewsData for this run.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import NewsdataProviderConfig
from ..http import FetchError, HttpClient
from ..schemas import RawItem
from .base import FetchRecord, NewsProvider, ProviderResult

log = logging.getLogger(__name__)

NEWSDATA_HOST = "newsdata.io"
PLACEHOLDER_PREFIX = "ONLY AVAILABLE IN"
QUOTA_CODES = {"apilimitexceeded", "apikeylimitexceeded"}   # daily credits gone
RATE_CODES = {"ratelimitexceeded"}                          # 30-credit / 15-minute window
BURST_CODES = {"toomanyrequests"}                           # short burst: brief backoff helps
BURST_RETRIES = 2
FATAL_HTTP = {401, 403}

_LANG = {"english": "en", "hindi": "hi", "french": "fr", "german": "de", "spanish": "es", "chinese": "zh",
         "arabic": "ar", "russian": "ru", "japanese": "ja", "portuguese": "pt", "italian": "it"}
_COUNTRY = {"india": "IN", "united states of america": "US", "united states": "US", "united kingdom": "GB",
            "china": "CN", "japan": "JP", "germany": "DE", "france": "FR", "australia": "AU", "canada": "CA",
            "singapore": "SG", "qatar": "QA", "russia": "RU", "pakistan": "PK", "bangladesh": "BD",
            "south africa": "ZA", "nigeria": "NG", "brazil": "BR", "hong kong": "HK", "israel": "IL",
            "united arab emirates": "AE", "saudi arabia": "SA", "ireland": "IE", "new zealand": "NZ"}


class NewsdataError(Exception):
    pass


def clean(value):
    """Drop paid-plan placeholder strings and empty values."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return None if not v or v.upper().startswith(PLACEHOLDER_PREFIX) else v
    if isinstance(value, list):
        out = [c for c in (clean(x) for x in value) if c]
        return out or None
    return value


def parse_pubdate(value: str | None, tz_name: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name) if tz_name and tz_name.upper() != "UTC" else timezone.utc)
        except (ZoneInfoNotFoundError, ValueError):
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def error_info(body) -> tuple[str | None, str]:
    """(code, message) from a NewsData error envelope; tolerant of dict/list/str shapes."""
    if not isinstance(body, dict):
        return None, str(body)[:300]
    res = body.get("results")
    if isinstance(res, list):
        res = res[0] if res else {}
    if isinstance(res, dict):
        return (str(res.get("code")) if res.get("code") else None), str(res.get("message") or res)[:300]
    return None, str(res or body)[:300]


class NewsdataProvider(NewsProvider):
    name = "newsdata"

    def __init__(self, http: HttpClient, cfg: NewsdataProviderConfig,
                 credits_used_today: Callable[[], int] | None = None,
                 domain_publishers: dict[str, str] | None = None, max_description_chars: int = 400):
        self.http = http
        self.cfg = cfg
        self.credits_used_today = credits_used_today or (lambda: 0)
        self.domain_publishers = domain_publishers or {}
        self.max_desc = max_description_chars
        self.last_credits_remaining: int | None = None
        self.skipped_reason: str | None = None
        http.set_host_interval(NEWSDATA_HOST, cfg.min_interval_seconds)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.cfg.api_key_env)

    def endpoint(self) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/{self.cfg.endpoint.strip('/')}"

    def build_params(self, query) -> dict[str, str]:
        params: dict[str, str] = {"language": self.cfg.language, "size": str(self.cfg.size)}
        if self.cfg.removeduplicate:
            params["removeduplicate"] = "1"
        if self.cfg.prioritydomain:
            params["prioritydomain"] = self.cfg.prioritydomain
        if query.q:
            params["qInMeta" if self.cfg.search_field == "qInMeta" else "q"] = query.q[:100]
        if query.category:
            params["category"] = query.category
        if query.country:
            params["country"] = query.country
        return params

    def fetch(self) -> ProviderResult:
        result = ProviderResult()
        if not self.api_key:
            self.skipped_reason = f"{self.cfg.api_key_env} not set"
            log.info("NewsData.io skipped: %s", self.skipped_reason)
            return result
        used = self.credits_used_today()
        budget = self.cfg.daily_credit_budget - used
        per_run = min(self.cfg.max_credits_per_run, budget)
        if per_run <= 0:
            self.skipped_reason = (f"daily credit budget reached ({used}/{self.cfg.daily_credit_budget} used today, UTC)")
            log.warning("NewsData.io skipped: %s", self.skipped_reason)
            return result
        spent = 0
        for q in self.cfg.queries:
            page: str | None = None
            for _ in range(self.cfg.max_pages_per_query):
                if spent >= per_run:
                    log.info("NewsData.io: per-run credit cap reached (%d)", per_run)
                    self.skipped_reason = f"stopped at per-run credit cap ({per_run})"
                    return result
                items, rec, page, stop = self.fetch_page(q, page)
                spent += 1
                result.items.extend(items)
                result.fetches.append(rec)
                if stop:
                    self.skipped_reason = rec.error
                    return result
                if not page:
                    break
        return result

    def fetch_page(self, query, page: str | None) -> tuple[list[RawItem], FetchRecord, str | None, bool]:
        """Returns (items, record, next_page, stop_all)."""
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        params = self.build_params(query)
        if page:
            params["page"] = page
        target = "newsdata " + " ".join(f"{k}={v}" for k, v in params.items() if k != "page") + (" (next page)" if page else "")

        def record(**kw) -> FetchRecord:
            return FetchRecord(provider=self.name, target=target, started_at=started,
                               duration_ms=int((time.monotonic() - t0) * 1000), **kw)

        headers = {"X-ACCESS-KEY": self.api_key or "", "Accept": "application/json"}
        for attempt in range(BURST_RETRIES + 1):
            try:
                # All parameters go in `params` (httpx replaces a query string already in the URL).
                resp = self.http.request("GET", self.endpoint(), params=params, headers=headers, check_robots=False,
                                         retry_statuses={500, 502, 503, 504})
            except FetchError as e:
                return [], record(ok=False, status_code=e.status_code, error=str(e)), None, False
            try:
                body = resp.json()
            except ValueError:
                return [], record(ok=False, status_code=resp.status_code,
                                  error=f"non-JSON response: {resp.text[:120]!r}"), None, False
            code, msg = error_info(body) if resp.status_code >= 400 else (None, "")
            if resp.status_code == 429 and (code or "").strip().lower() in BURST_CODES and attempt < BURST_RETRIES:
                self.http._sleep(2.0 * (2 ** attempt))
                continue
            break
        remaining = resp.headers.get("X-API-Limit-Remaining")
        if remaining and remaining.strip().isdigit():
            self.last_credits_remaining = int(remaining)
        window = resp.headers.get("X-RateLimit-Remaining")
        window_left = int(window) if window and window.strip().isdigit() else None

        if resp.status_code >= 400 or (isinstance(body, dict) and body.get("status") == "error"):
            code, msg = error_info(body)
            err = f"{(code or 'error').strip()}: {msg}"
            lowered = (code or "").strip().lower()
            stop = resp.status_code in FATAL_HTTP or resp.status_code == 429 or lowered in QUOTA_CODES | RATE_CODES
            if lowered in QUOTA_CODES:
                err = f"daily quota exhausted ({err})"
            elif resp.status_code == 429 or lowered in RATE_CODES:
                err = f"rate limited, stopping NewsData for this run ({err})"
            elif resp.status_code == 401:
                err = f"key rejected (401): {err}. Free keys reportedly lapse after ~30 days unused; regenerate it"
            elif resp.status_code == 403:
                err = f"access denied (403: plan or IP/domain restriction): {err}"
            log.warning("NewsData.io %s", err)
            return [], record(ok=False, status_code=resp.status_code, error=err[:500]), None, stop

        rows = body.get("results") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            return [], record(ok=False, status_code=resp.status_code, error="unexpected response shape"), None, False
        items = [it for row in rows if isinstance(row, dict) and (it := self._to_item(row, query.topic)) is not None]
        newest = max((i.published_at for i in items if i.published_at), default=None)
        next_page = body.get("nextPage") if isinstance(body.get("nextPage"), str) and body.get("nextPage") else None
        if window_left is not None and window_left <= 1:
            next_page = None  # don't page into the 15-minute rate limit
        return items, record(ok=True, status_code=resp.status_code, item_count=len(items), newest_item_at=newest), \
            next_page, False

    def _to_item(self, row: dict, topic: str | None) -> RawItem | None:
        if row.get("duplicate") is True:
            return None
        url, title = clean(row.get("link")), clean(row.get("title"))
        if not url or not title or not str(url).startswith(("http://", "https://")):
            return None
        source_url = clean(row.get("source_url")) or ""
        domain = source_url.split("//", 1)[-1].split("/", 1)[0].lower().removeprefix("www.") or None
        publisher = clean(row.get("source_name")) or self._publisher_for(domain) or clean(row.get("source_id"))
        published = parse_pubdate(clean(row.get("pubDate")), clean(row.get("pubDateTZ")))
        desc = clean(row.get("description"))
        if desc:
            desc = re.sub(r"\s*\[(?:\.\.\.|…)\]\s*$", "…", re.sub(r"\s+", " ", desc)).strip() or None
        if desc and len(desc) > self.max_desc:
            desc = desc[: self.max_desc].rsplit(" ", 1)[0] + "…"
        countries = clean(row.get("country")) or []
        country = _COUNTRY.get(str(countries[0]).lower(), str(countries[0])[:40]) if countries else None
        lang = str(clean(row.get("language")) or "").lower()
        creators = clean(row.get("creator")) or []
        cats = [str(c)[:40] for c in (clean(row.get("category")) or [])][:5]
        return RawItem(
            provider=self.name, title=str(title), url=str(url), description=desc, source_name=publisher,
            source_domain=domain, published_at=published, date_source="api" if published else "missing",
            language=_LANG.get(lang, lang[:2] or None), country=country,
            categories=([topic] if topic else []) + cats,
            author=", ".join(str(c) for c in creators[:3])[:300] or None, source_type="news",
        )

    def _publisher_for(self, domain: str | None) -> str | None:
        if not domain:
            return None
        for known, publisher in self.domain_publishers.items():
            if domain == known or domain.endswith("." + known):
                return publisher
        return None
