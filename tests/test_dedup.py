from datetime import datetime, timezone

from intelpod.pipeline.cluster import independent_sources
from intelpod.pipeline.dedup import deduplicate
from intelpod.pipeline.normalize import normalize_item
from intelpod.schemas import RawItem

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)


def art(title, url, source):
    return normalize_item(RawItem(provider="rss", title=title, url=url, source_name=source), now=NOW)


def test_duplicate_url_and_same_publisher_title_are_dropped():
    arts = [
        art("Oil prices jump after OPEC+ cut", "https://a.test/1", "Pub A"),
        art("Oil prices jump after OPEC+ cut", "https://a.test/1?utm_source=x", "Pub A"),
        art("Oil prices jump after OPEC+ cut", "https://a.test/other-url", "Pub A"),
    ]
    kept, stats = deduplicate(arts)
    assert len(kept) == 1
    assert stats.duplicate_url == 1 and stats.duplicate_title_same_publisher == 1


def test_against_existing_storage():
    first = art("Oil prices jump after OPEC+ cut", "https://a.test/1", "Pub A")
    kept, stats = deduplicate([art("Oil prices jump after OPEC+ cut", "https://a.test/1", "Pub A")], [first])
    assert kept == [] and stats.duplicate_url == 1


def test_syndicated_copies_share_group_and_count_once():
    a = art("Government unveils sweeping new export control rules for advanced chips", "https://a.test/1", "Pub A")
    b = art("Government unveils sweeping new export control rules for advanced chips", "https://b.test/9", "Pub B")
    c = art("Wire: Government unveils sweeping new export control rules for advanced chips", "https://c.test/3", "Pub C")
    d = art("Chipmakers say new rules will hit sales", "https://d.test/4", "Pub D")
    kept, stats = deduplicate([a, b, c, d])
    assert len(kept) == 4  # different publishers are kept...
    groups = {k.source_name: k.syndication_group for k in kept}
    assert groups["Pub A"] == groups["Pub B"] == groups["Pub C"]  # ...but grouped
    assert groups["Pub D"] != groups["Pub A"]
    assert stats.syndicated == 2
    assert independent_sources(kept) == 2  # A/B/C wire copy + D


def test_short_titles_are_not_fuzzy_merged():
    a = art("Markets rally strongly today", "https://a.test/1", "Pub A")
    b = art("Markets rally sharply today", "https://b.test/1", "Pub B")
    kept, _ = deduplicate([a, b])
    assert kept[0].syndication_group != kept[1].syndication_group
