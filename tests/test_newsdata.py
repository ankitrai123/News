"""NewsData.io provider against a MOCKED API (respx) with synthetic responses. No network."""
from datetime import datetime, timezone

import httpx
import pytest
import respx

from fixtures import synthetic_news as sn
from intelpod.config import HttpConfig, NewsdataProviderConfig, NewsdataQuery
from intelpod.http import HttpClient
from intelpod.providers.newsdata import NewsdataProvider, clean, error_info, parse_pubdate

URL = "https://newsdata.io/api/1/latest"


def provider(monkeypatch, key="nd-secret-key-123", used=0, **cfg):
    if key:
        monkeypatch.setenv("NEWSDATA_API_KEY", key)
    else:
        monkeypatch.delenv("NEWSDATA_API_KEY", raising=False)
    base = dict(min_interval_seconds=0, queries=[NewsdataQuery(topic="trade_policy", q="tariff OR trade deal"),
                                                 NewsdataQuery(topic="india", category="business", country="in")])
    base.update(cfg)
    http = HttpClient(HttpConfig(max_retries=1, backoff_base_seconds=0, per_host_min_interval_seconds=0,
                                 respect_robots_txt=False), "t", sleep=lambda s: None)
    return NewsdataProvider(http, NewsdataProviderConfig(**base), credits_used_today=lambda: used)


def test_helpers():
    assert clean("ONLY AVAILABLE IN PAID PLANS") is None and clean("  x ") == "x"
    assert clean(["ONLY AVAILABLE IN CORPORATE PLANS", "india"]) == ["india"] and clean([]) is None
    assert parse_pubdate("2026-10-16 16:00:00", "UTC") == datetime(2026, 10, 16, 16, tzinfo=timezone.utc)
    assert parse_pubdate("2026-10-16 21:30:00", "Asia/Kolkata") == datetime(2026, 10, 16, 16, tzinfo=timezone.utc)
    assert parse_pubdate("garbage", "UTC") is None
    assert error_info({"status": "error", "results": {"message": "Bad key", "code": "Unauthorized"}}) == ("Unauthorized", "Bad key")
    assert error_info({"status": "error", "results": [{"message": "x", "code": "UnsupportedParameter"}]})[0] == "UnsupportedParameter"


@respx.mock
def test_parses_results_ignores_placeholders_and_duplicates(monkeypatch):
    route = respx.get(URL).respond(200, json=sn.newsdata_json(), headers={"X-API-Limit-Remaining": "187"})
    p = provider(monkeypatch)
    res = p.fetch()
    assert route.call_count == 2 and all(f.ok for f in res.fetches)
    items = res.items[:2]
    fta, rupee = items
    assert fta.source_name == "ND Sample Times" and fta.source_domain == "nd-sample.test"
    assert fta.country == "IN" and fta.language == "en" and fta.date_source == "api"
    assert fta.published_at == datetime(2026, 10, 16, 16, tzinfo=timezone.utc)
    assert "trade_policy" in fta.categories and "business" in fta.categories
    assert fta.author == "Staff Writer"
    assert rupee.description is None  # placeholder dropped
    assert rupee.source_name == "othernd" and rupee.country == "US"
    assert all("Duplicate" not in i.title for i in res.items)
    assert p.last_credits_remaining == 187


@respx.mock
def test_key_sent_in_header_not_url(monkeypatch, caplog):
    route = respx.get(URL).respond(200, json=sn.newsdata_json())
    res = provider(monkeypatch).fetch()
    req = route.calls.last.request
    assert req.headers["X-ACCESS-KEY"] == "nd-secret-key-123"
    assert "nd-secret-key-123" not in str(req.url) and "apikey" not in str(req.url).lower()
    assert all("nd-secret-key-123" not in f.target for f in res.fetches)
    assert "nd-secret-key-123" not in caplog.text
    params = req.url.params
    assert params["language"] == "en" and params["size"] == "10" and params["removeduplicate"] == "1"
    assert params["category"] == "business" and params["country"] == "in"


@respx.mock
def test_pagination_respects_page_limit_and_credit_cap(monkeypatch):
    route = respx.get(URL).respond(200, json=sn.newsdata_json(next_page="1786940820853733956"))
    p = provider(monkeypatch, max_pages_per_query=3, max_credits_per_run=4)
    res = p.fetch()
    assert route.call_count == 4 and len(res.fetches) == 4  # 3 pages for query 1, then cap hit on query 2
    assert route.calls[1].request.url.params["page"] == "1786940820853733956"
    assert "cap" in p.skipped_reason


@respx.mock
def test_daily_budget_already_spent_makes_no_request(monkeypatch):
    route = respx.get(URL).respond(200, json=sn.newsdata_json())
    p = provider(monkeypatch, used=180, daily_credit_budget=180)
    res = p.fetch()
    assert route.call_count == 0 and res.items == [] and "budget" in p.skipped_reason


def test_missing_key_skips_without_requests(monkeypatch):
    p = provider(monkeypatch, key=None)
    res = p.fetch()
    assert res.fetches == [] and p.skipped_reason == "NEWSDATA_API_KEY not set"


@respx.mock
@pytest.mark.parametrize("status,code,phrase", [
    (429, "RateLimitExceeded", "rate limited"),
    (429, "ApiLimitExceeded", "quota exhausted"),
    (401, "Unauthorized", "key rejected"),
])
def test_stopping_errors_halt_all_queries(monkeypatch, status, code, phrase):
    route = respx.get(URL).respond(status, json={"status": "error", "results": {"message": "nope", "code": code}})
    p = provider(monkeypatch)
    res = p.fetch()
    assert route.call_count == 1  # no retry, no second query
    assert not res.fetches[0].ok and phrase in res.fetches[0].error and phrase in p.skipped_reason


@respx.mock
def test_parameter_error_on_one_query_does_not_stop_others(monkeypatch):
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(422, json={"status": "error", "results": {"message": "Unsupported", "code": "UnsupportedParameter"}}),
        httpx.Response(200, json=sn.newsdata_json())])
    res = provider(monkeypatch).fetch()
    assert route.call_count == 2 and not res.fetches[0].ok and res.fetches[1].ok and res.items


@respx.mock
def test_server_error_retried(monkeypatch):
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=sn.newsdata_json()),
                                             httpx.Response(200, json=sn.newsdata_json())])
    res = provider(monkeypatch).fetch()
    assert route.call_count == 3 and all(f.ok for f in res.fetches)


@respx.mock
def test_non_json_and_bad_shapes(monkeypatch):
    respx.get(URL).mock(side_effect=[httpx.Response(200, text="<html>maintenance</html>"),
                                     httpx.Response(200, json={"status": "success", "results": "oops"})])
    res = provider(monkeypatch).fetch()
    assert [f.ok for f in res.fetches] == [False, False] and res.items == []


def test_query_length_validated():
    with pytest.raises(ValueError):
        NewsdataQuery(topic="x", q="a" * 101)


@respx.mock
def test_burst_429_is_retried_but_window_429_is_not(monkeypatch):
    burst = {"status": "error", "results": {"message": "slow down", "code": "TooManyRequests"}}
    route = respx.get(URL).mock(side_effect=[httpx.Response(429, json=burst), httpx.Response(200, json=sn.newsdata_json()),
                                             httpx.Response(200, json=sn.newsdata_json())])
    res = provider(monkeypatch).fetch()
    assert route.call_count == 3 and all(f.ok for f in res.fetches)


@respx.mock
def test_error_code_with_trailing_space(monkeypatch):
    respx.get(URL).respond(429, json={"status": "error", "results": {"message": "x", "code": "ApiLimitExceeded "}})
    p = provider(monkeypatch)
    p.fetch()
    assert "quota exhausted" in p.skipped_reason


@respx.mock
def test_low_rate_window_stops_pagination(monkeypatch):
    route = respx.get(URL).respond(200, json=sn.newsdata_json(next_page="123"), headers={"X-RateLimit-Remaining": "1"})
    provider(monkeypatch, max_pages_per_query=3).fetch()
    assert route.call_count == 2  # one page per query; no paging into the rate limit


def test_description_truncation_marker_cleaned(monkeypatch):
    p = provider(monkeypatch)
    item = p._to_item({"link": "https://x.test/a", "title": "Some headline here", "pubDate": "2026-10-16 10:00:00",
                       "description": "Ministers met   today and agreed [...]"}, None)
    assert item.description == "Ministers met today and agreed…"


@respx.mock
@pytest.mark.parametrize("status", [429, 401, 403])
def test_non_json_stopping_errors_still_stop(monkeypatch, status):
    route = respx.get(URL).respond(status, text="<html>Blocked</html>")
    p = provider(monkeypatch)
    p.fetch()
    assert route.call_count == 1 and "stopping" in p.skipped_reason


@respx.mock
def test_curated_publisher_name_wins(monkeypatch):
    respx.get(URL).respond(200, json=sn.newsdata_json())
    p = provider(monkeypatch)
    p.domain_publishers = {"nd-sample.test": "ND Sample (curated)"}
    items = p.fetch().items
    assert items[0].source_name == "ND Sample (curated)"
