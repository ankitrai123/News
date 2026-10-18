"""REAL-NETWORK integration tests (not mocks). Run explicitly: `pytest -m live -v`.

They contact GDELT, the first configured RSS feed, NewsData.io (1 credit) and NVIDIA
(a few small requests). Tests needing a key are skipped when it isn't set (shell or .env).
They can fail when a source is down, rate-limits you, or retires a model.
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


def test_newsdata_latest(live_key):
    from intelpod.providers.newsdata import NewsdataProvider
    live_key("NEWSDATA_API_KEY")
    s = load_settings()
    cfg = s.ingestion.providers.newsdata
    with HttpClient(s.http, s.app.user_agent) as http:
        p = NewsdataProvider(http, cfg)
        items, rec, _, _ = p.fetch_page(cfg.queries[0], None)
    assert rec.ok, rec.error
    assert items and all(i.title and i.url.startswith("http") for i in items)
    assert all(not (i.description or "").upper().startswith("ONLY AVAILABLE") for i in items)


def test_nvidia_models_and_structured_chat(live_key):
    from pydantic import BaseModel

    from intelpod.llm.client import LLMClient
    live_key("NVIDIA_API_KEY")
    s = load_settings()
    s.llm.cache = False
    client = LLMClient(s.llm)
    models = client.list_models()
    assert models

    class Probe(BaseModel):
        capital: str

    obj, model = client.chat_json("event_review", [{"role": "user", "content":
                                  'Return JSON {"capital": "<capital of France>"}'}], Probe)
    assert "paris" in obj.capital.lower(), (model, obj)


def test_nvidia_embeddings(live_key):
    from intelpod.pipeline.embeddings import NvidiaEmbedder
    live_key("NVIDIA_API_KEY")
    s = load_settings()
    prov = s.llm.providers["nvidia"]
    e = NvidiaEmbedder(s.clustering.embeddings, prov.base_url, prov.api_key_env)
    out = e(["US tightens chip export controls on China", "Washington curbs semiconductor sales to Beijing",
             "Football final ends in penalty shootout"])
    assert out, e.last_error
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    assert dot(out[0], out[1]) > dot(out[0], out[2])  # paraphrases closer than an unrelated story


def test_gdelt_research_window_search():
    """One real GDELT research query over the last 14 days (keyless)."""
    from datetime import datetime, timedelta, timezone
    from intelpod.config import GdeltProviderConfig, HttpConfig, ResearchGdeltConfig
    from intelpod.http import HttpClient
    from intelpod.research.models import ResearchQuery
    from intelpod.research.providers import GdeltSearch
    now = datetime.now(timezone.utc)
    g = GdeltSearch(HttpClient(HttpConfig(timeout_seconds=60), "IntelPod live test"), ResearchGdeltConfig(),
                    GdeltProviderConfig())
    r = g.search(ResearchQuery(query_id="live", event_id="live", query_type="recent_history", provider="gdelt",
                               text='("interest rate" OR inflation) "central bank" sourcelang:english',
                               start=now - timedelta(days=14), end=now))
    assert r.status in ("ok", "rate_limited"), r.error
    if r.status == "ok":
        assert all(h.published_at is None or h.published_at >= now - timedelta(days=15) for h in r.hits)


def test_page_fetch_public_official_page():
    """Fetch one public central-bank page with robots.txt respected."""
    from intelpod.config import HttpConfig, ResearchPagesConfig
    from intelpod.http import HttpClient
    from intelpod.research.pages import PageFetcher
    p = PageFetcher(HttpClient(HttpConfig(), "IntelPod live test"), ResearchPagesConfig()).fetch(
        "https://www.federalreserve.gov/newsevents/pressreleases.htm")
    assert p.status in ("fetched", "metadata_only", "robots_disallowed"), p.error
