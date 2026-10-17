from datetime import datetime, timedelta, timezone

from intelpod.pipeline.normalize import normalize_item, validate_published_at
from intelpod.schemas import RawItem
from intelpod.textutil import canonicalize_url, normalize_title, strip_html, strip_publisher_suffix

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)


def item(**kw):
    base = dict(provider="rss", title="Central bank raises interest rates again", url="https://x.test/a")
    base.update(kw)
    return RawItem(**base)


def test_canonical_url_strips_tracking_fragment_www_and_sorts_query():
    a = canonicalize_url("http://www.Example.test/path/?utm_source=rss&b=2&a=1&fbclid=xyz#section")
    b = canonicalize_url("https://example.test/path?a=1&b=2")
    assert a == b == "https://example.test/path?a=1&b=2"


def test_canonical_url_strips_amp_suffix():
    assert canonicalize_url("https://example.test/story/amp/") == canonicalize_url("https://example.test/story")


def test_publisher_suffix_removed_only_when_it_names_the_source():
    assert strip_publisher_suffix("Rates rise - BBC News", "BBC", "bbc.co.uk") == "Rates rise"
    assert strip_publisher_suffix("Rates rise | Reuters", "Reuters", None) == "Rates rise"
    # A dash that is part of the headline is kept
    assert strip_publisher_suffix("Iran - US talks resume", "Example", "example.test") == "Iran - US talks resume"


def test_normalize_title_and_html():
    assert normalize_title("Fed’s “Big” Move: Rates CUT!") == "feds big move rates cut"
    assert strip_html("<p>Hello&nbsp;<b>world</b> &amp; co</p>") == "Hello world & co"


def test_dates_naive_future_and_ancient():
    naive = datetime(2026, 10, 16, 12, 0)
    d, ok = validate_published_at(naive, NOW)
    assert ok and d.tzinfo is not None and d.hour == 12
    d, ok = validate_published_at(NOW + timedelta(days=2), NOW)
    assert d is None and not ok
    d, ok = validate_published_at(datetime(1990, 1, 1, tzinfo=timezone.utc), NOW)
    assert d is None and not ok
    ist = timezone(timedelta(hours=5, minutes=30))
    d, ok = validate_published_at(datetime(2026, 10, 17, 9, 0, tzinfo=ist), NOW)
    assert d == datetime(2026, 10, 17, 3, 30, tzinfo=timezone.utc)


def test_normalize_item_handles_missing_fields():
    a = normalize_item(item(source_name=None, description=None, published_at=None), now=NOW)
    assert a.source_name == "x.test"
    assert a.published_at is None and a.date_source == "missing"
    assert a.effective_time == NOW
    assert a.content_access == "metadata"


def test_normalize_item_marks_invalid_future_date():
    a = normalize_item(item(published_at=NOW + timedelta(days=3), date_source="feed"), now=NOW)
    assert a.published_at is None and a.date_source == "invalid"


def test_normalize_rejects_empty_or_tiny_titles_and_truncates_description():
    assert normalize_item(item(title="  "), now=NOW) is None
    assert normalize_item(item(title="Live"), now=NOW) is None
    a = normalize_item(item(description="word " * 300), now=NOW, max_description_chars=100)
    assert len(a.description) <= 101 and a.description.endswith("…")


def test_description_equal_to_title_is_dropped():
    a = normalize_item(item(description="Central bank raises interest rates again"), now=NOW)
    assert a.description is None


def test_article_id_is_stable_across_tracking_variants():
    a = normalize_item(item(url="https://x.test/a?utm_campaign=z"), now=NOW)
    b = normalize_item(item(url="http://www.x.test/a/"), now=NOW)
    assert a.article_id == b.article_id
