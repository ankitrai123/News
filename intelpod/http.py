"""Polite HTTP client: timeouts, retries with backoff, per-host rate limits, robots.txt."""
from __future__ import annotations

import logging
import threading
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import HttpConfig

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RobotsDisallowed(FetchError):
    pass


class HttpClient:
    def __init__(self, cfg: HttpConfig, user_agent: str, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.cfg = cfg
        self.user_agent = user_agent
        self._client = httpx.Client(
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            transport=transport,
        )
        self._last_request: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def set_host_interval(self, host: str, seconds: float) -> None:
        self._host_interval[host] = seconds

    # -- politeness -------------------------------------------------------
    def _throttle(self, host: str) -> None:
        interval = self._host_interval.get(host, self.cfg.per_host_min_interval_seconds)
        with self._lock:
            last = self._last_request.get(host)
            now = time.monotonic()
            wait = 0.0 if last is None else max(0.0, interval - (now - last))
            self._last_request[host] = now + wait
        if wait > 0:
            self._sleep(wait)

    def allowed_by_robots(self, url: str) -> bool:
        if not self.cfg.respect_robots_txt:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        rp = RobotFileParser()
        try:
            self._throttle(urlsplit(origin).netloc)
            r = self._client.get(origin + "/robots.txt")
        except httpx.HTTPError as e:
            log.debug("robots.txt unavailable for %s (%s); treating as no restrictions", origin, e)
            return None
        if r.status_code in (401, 403):
            rp.disallow_all = True  # standard interpretation: access restricted
            return rp
        if r.status_code >= 400:
            return None  # no robots.txt => no restrictions
        rp.parse(r.text.splitlines())
        return rp

    # -- requests ---------------------------------------------------------
    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            check_robots: bool = True) -> httpx.Response:
        if check_robots and not self.allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        attempts = self.cfg.max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._throttle(host)
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except (httpx.ProxyError, httpx.UnsupportedProtocol) as e:
                # A proxy/policy denial or bad scheme will not fix itself on retry.
                raise FetchError(f"{type(e).__name__}: {e}") from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                log.warning("GET %s failed (%s) attempt %d/%d", host, type(e).__name__, attempt, attempts)
            else:
                if resp.status_code not in RETRY_STATUS:
                    return resp
                last_exc = FetchError(f"HTTP {resp.status_code}", resp.status_code)
                log.warning("GET %s -> %d attempt %d/%d", host, resp.status_code, attempt, attempts)
                if attempt < attempts:
                    retry_after = _retry_after_seconds(resp)
                    if retry_after is not None:
                        self._sleep(min(retry_after, 120))
                        continue
            if attempt < attempts:
                self._sleep(self.cfg.backoff_base_seconds * (2 ** (attempt - 1)))
        if isinstance(last_exc, FetchError):
            raise last_exc
        raise FetchError(f"request failed after {attempts} attempts: {last_exc}")


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None
