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
from .logging_setup import redact

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(redact(message))  # never carry an API key into logs, DB or reports
        self.status_code = status_code
        self.response: httpx.Response | None = None


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
        self._robots_lock = threading.Lock()
        self._robots_locks: dict[str, threading.Lock] = {}
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
        with self._robots_lock:
            origin_lock = self._robots_locks.setdefault(origin, threading.Lock())
        with origin_lock:  # feeds are fetched in parallel; load each robots.txt once per site
            if origin not in self._robots:
                self._robots[origin] = self._load_robots(origin)
            rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        """RFC 9309: 4xx (except 401/403) = no restrictions; 401/403, 5xx or unreachable = disallow
        everything. Redirects are followed only within the same site (www/apex, http->https), so a
        robots.txt can never send IntelPod to another (possibly internal) host."""
        rp = RobotFileParser()
        deny = RobotFileParser()
        deny.disallow_all = True
        url = origin + "/robots.txt"
        site = (urlsplit(origin).hostname or "").removeprefix("www.")
        for _ in range(5):
            try:
                self._throttle(urlsplit(url).netloc)
                with self._client.stream("GET", url, follow_redirects=False) as r:
                    status, location = r.status_code, r.headers.get("location")
                    body = b""
                    if not r.is_redirect:
                        for chunk in r.iter_bytes():
                            body += chunk
                            if len(body) > 512_000:  # RFC 9309 asks parsers to read at least 500 KiB
                                break
            except httpx.HTTPError as e:
                log.debug("robots.txt unreachable for %s (%s); treating as disallow-all", origin, e)
                return deny
            if 300 <= status < 400 and location:
                nxt = str(httpx.URL(url).join(location))
                host = (urlsplit(nxt).hostname or "").removeprefix("www.")
                if host != site or urlsplit(nxt).scheme not in ("http", "https"):
                    log.debug("robots.txt for %s redirects off-site; treating as disallow-all", origin)
                    return deny
                url = nxt
                continue
            if status in (401, 403) or status >= 500:
                return deny
            if status >= 400:
                return None  # no robots.txt => no restrictions
            rp.parse(body.decode("utf-8", errors="replace").splitlines())
            return rp
        return deny

    # -- requests ---------------------------------------------------------
    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            check_robots: bool = True) -> httpx.Response:
        return self.request("GET", url, params=params, headers=headers, check_robots=check_robots)

    def post(self, url: str, json: dict | None = None, headers: dict | None = None,
             timeout: float | None = None) -> httpx.Response:
        """POST to an API endpoint (robots.txt does not apply to documented APIs)."""
        return self.request("POST", url, json=json, headers=headers, check_robots=False, timeout=timeout)

    def get_limited(self, url: str, max_bytes: int, headers: dict | None = None,
                     url_check=None, max_redirects: int = 5, timeout: float | None = None) -> LimitedResponse:
        """GET a web page, reading at most `max_bytes`. Redirects are followed one hop at a time so
        each target is checked (robots.txt, and `url_check(url) -> reason | None`, e.g. no private
        hosts). Same throttle and retry rules as `request()`; never logs query strings."""
        hops: list[str] = []
        current = url
        for _ in range(max_redirects + 1):
            if url_check is not None:
                why = url_check(current)
                if why:
                    raise FetchError(f"blocked: {why}")
            if not self.allowed_by_robots(current):
                raise RobotsDisallowed(f"robots.txt disallows {current}")
            host = urlsplit(current).netloc
            attempts = self.cfg.max_retries + 1
            last_exc: Exception | None = None
            resp_meta = None
            for attempt in range(1, attempts + 1):
                self._throttle(host)
                try:
                    with self._client.stream("GET", current, headers=headers, follow_redirects=False,
                                        **({"timeout": timeout} if timeout else {})) as r:
                        if r.status_code in RETRY_STATUS and r.status_code != 429 and attempt < attempts:
                            last_exc = FetchError(f"HTTP {r.status_code}", r.status_code)
                            raise _Retry()
                        body = bytearray()
                        truncated = False
                        if not r.is_redirect:
                            for chunk in r.iter_bytes():
                                body.extend(chunk)
                                if len(body) > max_bytes:
                                    truncated = True
                                    del body[max_bytes:]
                                    break
                        resp_meta = (r.status_code, str(r.url), r.headers, bytes(body), truncated,
                                     r.headers.get("location"), r.is_redirect)
                    break
                except _Retry:
                    self._sleep(self.cfg.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                except (httpx.ProxyError, httpx.UnsupportedProtocol) as e:
                    raise FetchError(f"{type(e).__name__}: {e}") from e
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_exc = e
                    log.warning("GET %s failed (%s) attempt %d/%d", host, type(e).__name__, attempt, attempts)
                    if attempt < attempts and (self.cfg.retry_timeouts or not isinstance(e, httpx.TimeoutException)):
                        self._sleep(self.cfg.backoff_base_seconds * (2 ** (attempt - 1)))
                        continue
                    raise FetchError(f"{type(e).__name__}: request failed") from e
            if resp_meta is None:
                if isinstance(last_exc, FetchError):
                    raise last_exc
                raise FetchError(f"request failed: {last_exc}")
            status, final, hdrs, body, truncated, location, is_redirect = resp_meta
            if is_redirect and location:
                hops.append(current)
                current = str(httpx.URL(current).join(location))
                continue
            return LimitedResponse(status, final, hdrs, body, truncated, hops)
        raise FetchError("too many redirects")

    def request(self, method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
                json: dict | None = None, check_robots: bool = True, timeout: float | None = None,
                retry_statuses: set[int] | None = None) -> httpx.Response:
        """Send a request with throttling and retries.

        Retries timeouts, connection errors, 429 and 5xx (honouring Retry-After), or only
        `retry_statuses` when given. Never retries proxy/policy denials or other 4xx; a
        non-retried error status is returned to the caller. Log lines name only the host, so query
        strings (which may hold API keys) never reach the logs."""
        if check_robots and not self.allowed_by_robots(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        attempts = self.cfg.max_retries + 1
        last_exc: Exception | None = None
        extra = {"timeout": timeout} if timeout else {}
        retry_on = RETRY_STATUS if retry_statuses is None else retry_statuses
        for attempt in range(1, attempts + 1):
            self._throttle(host)
            try:
                resp = self._client.request(method, url, params=params, headers=headers, json=json, **extra)
            except (httpx.ProxyError, httpx.UnsupportedProtocol) as e:
                # A proxy/policy denial or bad scheme will not fix itself on retry.
                raise FetchError(f"{type(e).__name__}: {e}") from e
            except httpx.LocalProtocolError as e:
                # e.g. an illegal header value: the message would contain the header (an API key).
                raise FetchError(f"{type(e).__name__}: invalid request (check API key formatting)") from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                log.warning("%s %s failed (%s) attempt %d/%d", method, host, type(e).__name__, attempt, attempts)
                if isinstance(e, httpx.TimeoutException) and not self.cfg.retry_timeouts:
                    raise FetchError(f"{type(e).__name__}: timed out") from e
            else:
                if resp.status_code not in retry_on:
                    return resp
                last_exc = FetchError(f"HTTP {resp.status_code}", resp.status_code)
                last_exc.response = resp
                log.warning("%s %s -> %d attempt %d/%d", method, host, resp.status_code, attempt, attempts)
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


class LimitedResponse:
    """Status, headers and at most `max_bytes` of a body (see `HttpClient.get_limited`)."""

    def __init__(self, status_code: int, url: str, headers: httpx.Headers, body: bytes, truncated: bool,
                 redirects: list[str]):
        self.status_code = status_code
        self.url = url
        self.headers = headers
        self.body = body
        self.truncated = truncated
        self.redirects = redirects

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    def text(self) -> str:
        enc = "utf-8"
        m = (self.headers.get("content-type") or "").lower().split("charset=")
        if len(m) > 1:
            enc = m[1].split(";")[0].strip() or "utf-8"
        return self.body.decode(enc, errors="replace")


class _Retry(Exception):
    pass


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
