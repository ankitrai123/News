"""REAL-NETWORK integration tests (not mocks). Run explicitly: `pytest -m live`.

They hit GDELT and the first enabled feed in config/feeds.yaml, so they need internet
access and can fail when a source is down or rate-limits you.
"""
import pytest

from intelpod.config import load_settings
from intelpod.http import HttpClient
from intelpod.providers import GdeltProvider, RssProvider, load_feeds

pytestmark = pytest.mark.live


def test_gdelt_returns_current_articles():
    s = load_settings()
    with HttpClient(s.http, s.app.user_agent) as http:
        items, rec = GdeltProvider(http, s.ingestion.providers.gdelt).fetch_query('(tariff OR "interest rate")')
    assert rec.ok, rec.error
    assert items and all(i.url.startswith("http") for i in items)


def test_first_feed_returns_items():
    s = load_settings()
    feed = load_feeds(s.resolve(s.ingestion.feeds_file))[0]
    with HttpClient(s.http, s.app.user_agent) as http:
        items, rec = RssProvider(http, [feed], use_conditional_get=False).fetch_feed(feed)
    assert rec.ok, rec.error
    assert items
