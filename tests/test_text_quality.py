"""Text/topic/feed fixes from user testing (#10-#13)."""
from datetime import datetime, timedelta, timezone

import respx

from fixtures import synthetic_news as sn
from intelpod.config import ClusteringConfig
from intelpod.pipeline.cluster import Cluster, build_docs, summarize_cluster
from intelpod.pipeline.normalize import normalize_item
from intelpod.providers.rss import FeedSpec, RssProvider, parse_loose_date
from intelpod.schemas import RawItem
from intelpod.textutil import canonical_term, find_countries, looks_like_table


def art(title, desc=None, url=None):
    return normalize_item(RawItem(provider="rss", title=title, description=desc, url=url or f"https://x.test/{abs(hash(title))}",
                                  source_name="P", published_at=sn.NOW), now=sn.NOW)


def test_currency_is_not_a_country():
    assert find_countries("Exports rose to US$ 5.2 billion and the deficit to US $3 bn") == []
    assert find_countries("US imposes tariffs") == ["United States"]


def test_table_dump_description_dropped():
    rbi = ("Item 2024-25 2025-26 Current account -9.7 -10.2 Trade balance -63.6 -72.1 Services 39.9 45.2 "
           "Primary income -12.0 -11.4 Net capital flows 18.3 21.0")
    assert looks_like_table(rbi)
    a = art("RBI releases balance of payments data for Q1 2026-27", rbi)
    assert a.description is None
    prose = art("RBI releases balance of payments data", "India's current account deficit narrowed to 1.1% of GDP.")
    assert prose.description


def test_subtopic_labels_are_canonical(taxonomy):
    cl = Cluster()
    for d in build_docs([art("New tariffs on steel imports announced"), art("Tariff on steel imports to rise"),
                         art("Steel tariffs widen to more imports")], taxonomy):
        cl.add(d)
    summ = summarize_cluster(cl, taxonomy)
    assert summ["subtopic"] == "tariff"
    for raw, want in [("missiles", "missile"), ("drones", "drone"), ("elections", "election"), ("chips", "chip"),
                      ("rare earths", "rare earth"), ("ai", "artificial intelligence"), ("peace talks", "peace talks")]:
        assert canonical_term(raw, taxonomy.label_aliases) == want


def test_headline_decides_topic_over_side_mentions(taxonomy):
    cl = Cluster()
    for d in build_docs([art("India, Canada sign commercial deals",
                             "The agreements also cover AI research and software services.")], taxonomy):
        cl.add(d)
    assert summarize_cluster(cl, taxonomy)["topic"] == "trade_policy"


def test_loose_feed_dates_and_timezones():
    assert parse_loose_date("17 Oct 2026 18:30:00 IST") == datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)
    assert parse_loose_date("Fri, 17 Oct 2026 10:15", "Asia/Tokyo") == datetime(2026, 10, 17, 1, 15, tzinfo=timezone.utc)
    assert parse_loose_date("no date here") is None


@respx.mock
def test_rss_items_with_nonstandard_dates_get_dates():
    feed = FeedSpec(name="RBI", url="https://rbi.test/rss.xml", publisher="RBI", country="IN", source_type="official")
    xml = ('<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
           '<item><title>RBI announces auction of government securities</title><link>https://rbi.test/a</link>'
           '<pubDate>Oct 17, 2026 18:30</pubDate></item></channel></rss>')
    respx.get(feed.url).respond(200, text=xml)
    from intelpod.config import HttpConfig
    from intelpod.http import HttpClient
    http = HttpClient(HttpConfig(max_retries=0, per_host_min_interval_seconds=0, respect_robots_txt=False), "t")
    items, rec = RssProvider(http, [feed]).fetch_feed(feed)
    assert items[0].published_at == datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)  # IST -> UTC
    assert items[0].date_source == "feed"


def test_failing_feeds_are_paused_then_retried(settings):
    from intelpod.pipeline.runner import Phase1Pipeline
    from intelpod.providers.base import FetchRecord
    from intelpod.providers.rss import load_feeds
    pipe = Phase1Pipeline(settings)
    feeds = load_feeds(settings.resolve(settings.ingestion.feeds_file))
    bad = feeds[0]
    now = datetime.now(timezone.utc)
    pipe.repo.log_fetches(None, [FetchRecord(provider="rss", target=bad.url, started_at=now, duration_ms=1, ok=False,
                                             status_code=404, error="HTTP 404")] * 5)
    active, paused = pipe._active_feeds(feeds)
    assert bad.name in paused and bad not in active
    # a day later it is tried again
    from intelpod.db import FeedStateRow
    with pipe.repo.db.session() as s:
        s.get(FeedStateRow, bad.url).last_checked_at = now - timedelta(hours=25)
    active, paused = pipe._active_feeds(feeds)
    assert bad in active and not paused


def test_dates_flag_only_when_no_exact_dates(taxonomy):
    from intelpod.config import RankingConfig
    from intelpod.pipeline.cluster import Clusterer, build_candidates
    from intelpod.pipeline.rank import Ranker
    g = RawItem(provider="gdelt", title="Chip export rules widened for China firms", url="https://g.test/1",
                source_name="G", published_at=sn.NOW, date_source="gdelt_seendate")
    r = RawItem(provider="rss", title="Chip export rules widened for Chinese firms", url="https://r.test/1",
                source_name="R", published_at=sn.NOW, date_source="feed")
    arts = [normalize_item(x, now=sn.NOW) for x in (g, r)]
    cfg = ClusteringConfig()
    cands = build_candidates(Clusterer(cfg).cluster(build_docs(arts, taxonomy)), taxonomy, cfg)
    ranked = Ranker(RankingConfig(), taxonomy).rank(cands, {a.article_id: a for a in arts})
    mixed = next(x for x in ranked if x.article_count == 2)
    assert "dates_approximate" not in mixed.flags
    only_g = Ranker(RankingConfig(), taxonomy).rank(build_candidates(
        Clusterer(cfg).cluster(build_docs(arts[:1], taxonomy)), taxonomy, cfg), {arts[0].article_id: arts[0]})
    assert "dates_approximate" in only_g[0].flags
