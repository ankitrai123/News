"""Fetch result pages politely and read their metadata and main text.

Rules (never relaxed):
  * only http(s) URLs on public hosts: localhost, single-label names and private, loopback,
    link-local or reserved addresses are refused, after every redirect too;
  * robots.txt is honoured for every page and redirect target;
  * pages whose publisher says the content is not free (schema.org `isAccessibleForFree: false`,
    a login/subscribe wall, HTTP 401/402/403) are used for metadata only — nothing is extracted;
  * `noai`, `noarchive`, `nosnippet`, `none` robots directives (meta tag or X-Robots-Tag header)
    and a W3C TDM reservation (`tdm-reservation: 1`) also mean metadata only;
  * at most `max_bytes` are read; non-HTML documents (PDF, ...) are metadata only.

The extracted text is returned in memory (`PageInfo.text`, never serialised) so short verbatim
extracts can be taken; it is never stored.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlsplit

from dateutil import parser as dateparser

from ..config import ResearchPagesConfig
from ..http import FetchError, HttpClient, RobotsDisallowed
from .models import PageInfo

log = logging.getLogger(__name__)

HTML_TYPES = ("text/html", "application/xhtml+xml")
WALL_PATHS = ("/login", "/signin", "/sign-in", "/subscribe", "/subscription", "/paywall", "/register", "/account/")
WALL_TEXT = re.compile(r"\b(subscribe to (continue|read)|subscribers only|log in to (continue|read)|"
                       r"sign in to (continue|read)|this article is for subscribers|create a free account to)", re.I)
BLOCKING_DIRECTIVES = ("noai", "noarchive", "nosnippet", "none", "noimageai")
PRIMARY_LINK = re.compile(r"\.(gov|gouv|gob|go|int|mil|edu|ac)(\.[a-z]{2})?$|\.(gov|nic)\.in$|"
                          r"(federalreserve|ecb\.europa|rbi\.org|bankofengland|imf|worldbank|wto|oecd|bis|un|europa)\.",
                          re.I)
CORRECTIONS = re.compile(r"correction|editorial[- ]standards|ethics[- ]policy|fact[- ]?check[- ]policy", re.I)

_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'([a-zA-Z_:\-]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')
_LDJSON = re.compile(r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.I | re.S)
_TIME = re.compile(r"<time[^>]+datetime\s*=\s*[\"']([^\"']+)[\"']", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_LINK = re.compile(r"<a\s[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def _wall_path(path: str) -> bool:
    segs = {s for s in path.lower().split("/") if s}
    return bool(segs & {p.strip("/") for p in WALL_PATHS})


def url_problem(url: str, resolver=socket.getaddrinfo) -> str | None:
    """Why IntelPod must not fetch this URL, or None. DNS failure is allowed (a proxy may resolve it)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "not an http(s) URL"
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host \
            and not _is_ip(host):
        return "local host name"
    if _is_ip(host):
        return None if ipaddress.ip_address(host).is_global else "private or reserved address"
    try:
        infos = resolver(host, None)
    except (OSError, UnicodeError):
        return None
    addrs = {i[4][0] for i in infos if i and len(i) > 4}
    if addrs and not all(_is_global(a) for a in addrs):
        return "host resolves to a private address"
    return None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _is_global(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_global
    except ValueError:
        return False


def _attrs(tag: str) -> dict[str, str]:
    return {k.lower(): unescape(v.strip("\"'")) for k, v in _ATTR.findall(tag)}


def parse_meta(html: str) -> dict[str, str]:
    """name/property -> content for <meta> tags (first occurrence wins)."""
    out: dict[str, str] = {}
    for tag in _META.findall(html):
        a = _attrs(tag)
        key = (a.get("property") or a.get("name") or a.get("http-equiv") or "").lower()
        if key and "content" in a and key not in out:
            out[key] = a["content"]
    return out


def parse_jsonld(html: str) -> dict:
    """The article-like JSON-LD object (handles lists and @graph): type, dates, free access, author."""
    best: dict = {}
    not_free = False
    for block in _LDJSON.findall(html):
        try:
            data = json.loads(block.strip(), strict=False)
        except json.JSONDecodeError:
            continue
        for obj in _walk(data):
            if _free_flag(obj) is False:
                not_free = True  # on any node (Article, WebPage, hasPart) of any block
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            types = [t for t in types if isinstance(t, str)]
            if any(t.endswith(("Article", "Report", "PressRelease", "BlogPosting", "NewsArticle")) or t == "WebPage"
                   for t in types):
                art = next((t for t in types if t != "WebPage"), types[0] if types else None)
                score = 2 if art and art != "WebPage" else 1
                if score > best.get("_score", 0):
                    best = {"_score": score, "type": art, "datePublished": obj.get("datePublished"),
                            "dateModified": obj.get("dateModified"), "headline": obj.get("headline"),
                            "description": obj.get("description"),
                            "isAccessibleForFree": _free_flag(obj),
                            "author": _name(obj.get("author")), "publisher": _name(obj.get("publisher"))}
    best.pop("_score", None)
    if not_free:
        best["isAccessibleForFree"] = False
    return best


def _walk(data):
    if isinstance(data, list):
        for x in data:
            yield from _walk(x)
    elif isinstance(data, dict):
        yield data
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if isinstance(data.get(key), (list, dict)):
                yield from _walk(data[key])


def _free_flag(obj: dict) -> bool | None:
    v = obj.get("isAccessibleForFree")
    parts = obj.get("hasPart")
    parts = parts if isinstance(parts, list) else [parts] if isinstance(parts, dict) else []
    for p in parts:
        if isinstance(p, dict) and str(p.get("isAccessibleForFree")).lower() in ("false", "0"):
            return False
    if v is None:
        return None
    return str(v).lower() not in ("false", "0", "no")


def _name(v) -> str | None:
    if isinstance(v, list):
        names = [n for n in (_name(x) for x in v) if n]
        return ", ".join(names[:3]) or None
    if isinstance(v, dict):
        return v.get("name") if isinstance(v.get("name"), str) else None
    return v if isinstance(v, str) else None


def parse_date(value, now: datetime | None = None) -> datetime | None:
    """A full date (with day) as aware UTC; None for unparseable, future (> now + 2h) or pre-1995 values."""
    if not value or not isinstance(value, str):
        return None
    try:
        a = dateparser.parse(value, default=datetime(2001, 1, 1))
        b = dateparser.parse(value, default=datetime(2002, 2, 2))
    except (ValueError, OverflowError, TypeError):
        return None
    if a is None or b is None or a.date() != b.date():
        return None
    dt = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    if dt > now + timedelta(hours=2) or dt.year < 1995:
        return None
    return dt


def robots_directives(meta: dict[str, str], headers) -> set[str]:
    vals = [meta.get(k, "") for k in ("robots", "googlebot", "bingbot")]
    vals += headers.get_list("x-robots-tag") if hasattr(headers, "get_list") else [headers.get("x-robots-tag") or ""]
    out = set()
    for v in vals:
        for d in re.split(r",\s*", (v or "").lower()):
            d = re.sub(r"\s+", "", d)
            if d.startswith(("googlebot:", "bingbot:", "*:")):  # "googlebot: noarchive"
                d = d.split(":", 1)[1]
            if d in ("max-snippet:0",):
                d = "nosnippet"
            for part in d.split():
                if part:
                    out.add(part)
    return out


def tdm_reserved(meta: dict[str, str], headers) -> bool:
    """W3C TDM Reservation Protocol: the rightsholder reserves text-and-data-mining rights."""
    return (headers.get("tdm-reservation") or meta.get("tdm-reservation") or "").strip() == "1"


def main_text(html: str, url: str) -> tuple[str | None, str | None]:
    """(text, page type) via trafilatura; falls back to paragraph text if it is unavailable."""
    try:
        import trafilatura  # noqa: PLC0415 - optional at import time, required in practice
    except ImportError:
        return _fallback_text(html), None
    try:
        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, favor_precision=True,
                                          include_comments=False, include_tables=False)
    except Exception as e:  # noqa: BLE001 - a parser bug on one page must not stop research
        log.debug("trafilatura failed on %s: %s", url, e)
        return _fallback_text(html), None
    if doc is None:
        return None, None
    d = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
    return d.get("text") or None, d.get("pagetype") or None


def _fallback_text(html: str) -> str | None:
    body = re.search(r"<(article|main)[^>]*>(.*?)</\1>", html, re.I | re.S)
    scope = body.group(2) if body else html
    paras = [" ".join(unescape(_TAG.sub(" ", p)).split()) for p in re.findall(r"<p[^>]*>(.*?)</p>", scope, re.I | re.S)]
    paras = [p for p in paras if len(p.split()) >= 6]
    return "\n".join(paras) or None


def link_stats(html: str, own_host: str) -> tuple[int, bool]:
    primary, corrections = 0, False
    for href, label in _LINK.findall(html):
        h = (urlsplit(unescape(href)).hostname or "").lower()
        text = _TAG.sub(" ", label)
        if CORRECTIONS.search(href) or CORRECTIONS.search(text):
            corrections = True
        if h and h != own_host and (PRIMARY_LINK.search(h) or href.lower().split("?")[0].endswith(".pdf")):
            primary += 1
    return primary, corrections


class PageFetcher:
    def __init__(self, http: HttpClient, cfg: ResearchPagesConfig, resolver=socket.getaddrinfo,
                 no_extract_domains: tuple[str, ...] = ()):
        self.http = http
        self.cfg = cfg
        self.resolver = resolver
        self.no_extract_domains = tuple(d.lower() for d in no_extract_domains)

    def fetch(self, url: str) -> PageInfo:
        try:
            return self._fetch(url)
        except Exception as e:  # noqa: BLE001 - never raise: the status explains the failure
            log.debug("page %s failed", url, exc_info=True)
            return PageInfo(url=url, status="error", error=f"{type(e).__name__}: {str(e)[:200]}")

    def _fetch(self, url: str) -> PageInfo:
        why = url_problem(url, self.resolver)
        if why:
            return PageInfo(url=url, status="blocked_host", error=why)
        try:
            resp = self.http.get_limited(url, self.cfg.max_bytes,
                                         headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
                                         url_check=lambda u: url_problem(u, self.resolver),
                                         timeout=self.cfg.timeout_seconds)
        except RobotsDisallowed:
            return PageInfo(url=url, status="robots_disallowed", error="robots.txt disallows this page")
        except FetchError as e:
            msg = str(e)
            if msg.startswith("blocked:"):
                return PageInfo(url=url, status="blocked_host", error=msg)
            if "robots.txt" in msg:
                return PageInfo(url=url, status="robots_disallowed", error=msg)
            return PageInfo(url=url, status="error", http_status=e.status_code, error=msg[:300])
        final = resp.url
        path = urlsplit(final).path.lower()
        info = PageInfo(url=url, final_url=final, status="fetched", http_status=resp.status_code)
        if resp.status_code in (401, 402, 403):
            body = resp.text()[:20000] if resp.body else ""
            walled = resp.status_code == 402 or bool(WALL_TEXT.search(body)) or _wall_path(path)
            info.status, info.error = ("paywalled" if walled else "error"), f"HTTP {resp.status_code}"
            return info
        if resp.status_code >= 400:
            info.status, info.error = "error", f"HTTP {resp.status_code}"
            return info
        if resp.redirects and _wall_path(path):
            info.status, info.error = "paywalled", "redirected to a login or subscription page"
            return info
        if resp.content_type and resp.content_type not in HTML_TYPES:
            info.status, info.error = "metadata_only", f"not a web page ({resp.content_type})"
            return info
        html = resp.text()
        meta = parse_meta(html)
        ld = parse_jsonld(html)
        self._metadata(info, html, meta, ld)
        directives = robots_directives(meta, resp.headers)
        blocked = sorted(d for d in directives if d in BLOCKING_DIRECTIVES)
        host = (urlsplit(final).hostname or "").lower().removeprefix("www.")
        if ld.get("isAccessibleForFree") is False:
            info.status, info.error = "paywalled", "publisher marks the content as not free"
            return info
        if blocked:
            info.status, info.error = "metadata_only", "page opts out: " + ", ".join(blocked)
            return info
        if tdm_reserved(meta, resp.headers):
            info.status, info.error = "metadata_only", "publisher reserves text-and-data-mining rights"
            return info
        if any(host == d or host.endswith("." + d) for d in self.no_extract_domains):
            info.status, info.error = "metadata_only", "publisher's terms exclude automated text use (configured)"
            return info
        text, pagetype = main_text(html, final)
        if pagetype and not info.schema_type:
            info.schema_type = pagetype
        if text and WALL_TEXT.search(text[:3000]) and len(text.split()) < 250:
            info.status, info.error = "paywalled", "only a subscription teaser is visible"
            return info
        if not text:
            info.status, info.error = "metadata_only", "no readable article text"
            return info
        info.text = text
        info.word_count = len(text.split())
        if resp.truncated:
            info.error = f"page larger than {self.cfg.max_bytes} bytes; read the first part only"
        return info

    def _metadata(self, info: PageInfo, html: str, meta: dict, ld: dict) -> None:
        t = _TITLE.search(html)
        info.title = (ld.get("headline") if isinstance(ld.get("headline"), str) else None) or meta.get("og:title") or \
            (" ".join(unescape(_TAG.sub(" ", t.group(1))).split()) if t else None)
        desc = ld.get("description") if isinstance(ld.get("description"), str) else None
        info.description = (desc or meta.get("og:description") or meta.get("description") or "")[:400] or None
        info.author = ld.get("author") or meta.get("author") or meta.get("article:author") or None
        info.sitename = meta.get("og:site_name") or ld.get("publisher") or None
        info.schema_type = ld.get("type") if ld.get("type") not in (None, "WebPage") else None
        info.accessible_for_free = ld.get("isAccessibleForFree")
        for value, where in ((ld.get("datePublished"), "json-ld datePublished"),
                             (meta.get("article:published_time"), "meta article:published_time"),
                             (meta.get("og:article:published_time"), "meta og:article:published_time"),
                             (meta.get("date") or meta.get("dc.date") or meta.get("dcterms.date"), "meta date")):
            d = parse_date(value)
            if d:
                info.published_at, info.date_found_in = d, where
                break
        if info.published_at is None:
            m = _TIME.search(html)
            d = parse_date(m.group(1)) if m else None
            if d:
                info.published_at, info.date_found_in = d, "<time> element"
        info.modified_at = parse_date(ld.get("dateModified") or meta.get("article:modified_time"))
        own = (urlsplit(info.final_url or info.url).hostname or "").lower()
        info.outbound_primary_links, info.has_corrections_link = link_stats(html, own)
