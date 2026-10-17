"""Provider tests use respx to mock HTTP (no network). See tests/test_live.py for real-network checks."""
import httpx
import pytest
import respx

from fixtures import synthetic_news as sn
from intelpod.config import GdeltProviderConfig, GdeltQuery, HttpConfig
from intelpod.http import HttpClient, RobotsDisallowed
from intelpod.providers.gdelt import GDELT_DOC_URL, GdeltError, GdeltProvider, parse_artlist, parse_seendate
from intelpod.providers.rss import FeedSpec, RssProvider

FEED = FeedSpec(name="Example World Service", url="https://world.example-news.test/rss.xml",
                publisher="Example World Service", domain="world.example-news.test", country="GB")


def client(**kw):
    cfg = HttpConfig(max_retries=kw.pop("retries", 1), backoff_base_seconds=0, per_host_min_interval_seconds=0,
                     respect_robots_txt=kw.pop("robots", False))
    return HttpClient(cfg, "IntelPodTest/0.1", sleep=lambda s: None)


@respx.mock
def test_rss_parses_items_and_strips_html():
    respx.get(FEED.url).respond(200, text=sn.rss_xml("world"), headers={"ETag": '"v1"'})
    items, rec = RssProvider(client(), [FEED]).fetch_feed(FEED)
    assert rec.ok and rec.item_count == len(items) > 5 and rec.etag == '"v1"'
    first = items[0]
    assert first.source_name == "Example World Service" and "<p>" not in (first.description or "")
    assert first.published_at.tzinfo is not None and first.date_source == "feed"


@respx.mock
def test_rss_conditional_get_304():
    route = respx.get(FEED.url).respond(304)
    items, rec = RssProvider(client(), [FEED], validators=lambda u: ('"v1"', None)).fetch_feed(FEED)
    assert items == [] and rec.ok and rec.not_modified
    assert route.calls.last.request.headers["If-None-Match"] == '"v1"'


@respx.mock
def test_rss_malformed_feed_is_recorded_not_raised():
    respx.get(FEED.url).respond(200, text="<html><body>Not a feed</body>")
    items, rec = RssProvider(client(), [FEED]).fetch_feed(FEED)
    assert items == [] and not rec.ok and "unparseable" in rec.error


@respx.mock
def test_rss_server_error_retries_then_fails():
    route = respx.get(FEED.url).respond(503)
    items, rec = RssProvider(client(retries=2), [FEED]).fetch_feed(FEED)
    assert route.call_count == 3 and not rec.ok and rec.status_code == 503


@respx.mock
def test_rss_timeout_is_recorded():
    respx.get(FEED.url).mock(side_effect=httpx.ConnectTimeout("boom"))
    items, rec = RssProvider(client(), [FEED]).fetch_feed(FEED)
    assert not rec.ok and "failed after" in rec.error


@respx.mock
def test_robots_txt_is_respected():
    respx.get("https://world.example-news.test/robots.txt").respond(200, text="User-agent: *\nDisallow: /rss.xml\n")
    feed_route = respx.get(FEED.url).respond(200, text=sn.rss_xml("world"))
    c = client(robots=True)
    with pytest.raises(RobotsDisallowed):
        c.get(FEED.url)
    items, rec = RssProvider(c, [FEED]).fetch_feed(FEED)
    assert not rec.ok and "robots" in rec.error and feed_route.call_count == 0


@respx.mock
def test_retry_after_header_is_honoured():
    sleeps = []
    route = respx.get("https://x.test/a").mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}),
                                                           httpx.Response(200, text="ok")])
    c = HttpClient(HttpConfig(max_retries=2, backoff_base_seconds=0, per_host_min_interval_seconds=0,
                              respect_robots_txt=False), "t", sleep=sleeps.append)
    assert c.get("https://x.test/a").text == "ok" and 7.0 in sleeps and route.call_count == 2


def gdelt(**kw):
    cfg = GdeltProviderConfig(min_interval_seconds=0, queries=[GdeltQuery(topic="trade_policy", query="(tariff)")], **kw)
    return GdeltProvider(client(), cfg, {"gdelt-sample.test": "GDELT Sample Publisher"})


@respx.mock
def test_gdelt_parses_articles_and_skips_bad_rows():
    route = respx.get(GDELT_DOC_URL).respond(200, text=sn.gdelt_json())
    res = gdelt().fetch()
    assert len(res.items) == 2 and res.fetches[0].ok
    it = res.items[0]
    assert it.source_name == "GDELT Sample Publisher" and it.date_source == "gdelt_seendate"
    assert it.language == "en" and it.description is None
    q = route.calls.last.request.url.params
    assert q["mode"] == "ArtList" and q["format"] == "json" and "sourcelang:english" in q["query"]


@respx.mock
def test_gdelt_rate_limit_text_is_an_error_not_a_crash():
    respx.get(GDELT_DOC_URL).respond(200, text=sn.GDELT_RATE_LIMIT_TEXT)
    res = gdelt().fetch()
    assert res.items == [] and not res.fetches[0].ok and "non-JSON" in res.fetches[0].error


def test_gdelt_parsers():
    assert parse_artlist("") == []
    assert parse_artlist('{"articles": [{"url": "u"}, 5]}') == [{"url": "u"}]
    with pytest.raises(GdeltError):
        parse_artlist('{"articles": [')
    assert parse_seendate("20261017T031500Z").hour == 3
    assert parse_seendate("garbage") is None


@respx.mock
def test_proxy_denial_is_not_retried():
    route = respx.get(FEED.url).mock(side_effect=httpx.ProxyError("403 Forbidden"))
    items, rec = RssProvider(client(retries=3), [FEED]).fetch_feed(FEED)
    assert route.call_count == 1 and not rec.ok and "ProxyError" in rec.error
