"""SYNTHETIC TEST FIXTURES — NOT REAL NEWS.

Every headline below was written for automated tests. Publishers are fictional and
all URLs use the reserved `.test` TLD so nothing here can be mistaken for real
reporting. Dates are generated relative to the test's fixed `NOW`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)

# (feed key, hours ago, title, description, url path)
STORIES = [
    # A: one event, four phrasings, three publishers (+ a syndicated copy)
    ("world", 10, "United States announces new semiconductor export controls on China",
     "The Commerce Department unveiled export controls restricting advanced chip sales to Chinese firms.", "/a/us-chip-controls"),
    ("biz", 9, "Washington tightens restrictions on chip exports",
     "New US curbs target advanced semiconductors and chipmaking tools bound for China.", "/b/washington-chip-curbs"),
    ("india", 8, "New American measures target China's chip industry",
     "US export controls on semiconductors could reshape supply chains, Indian electronics makers say.", "/i/american-chip-measures"),
    ("biz", 7, "Wire: United States announces new semiconductor export controls on China",
     "The Commerce Department unveiled export controls restricting advanced chip sales to Chinese firms.", "/b/wire-us-chip-controls"),
    # B: RBI rate cut
    ("india", 6, "RBI cuts repo rate by 25 basis points as inflation cools",
     "The Reserve Bank of India lowered its key interest rate, citing easing consumer prices; the rupee was steady.", "/i/rbi-cuts-repo"),
    ("biz", 5, "India's central bank lowers interest rate to support growth",
     "The RBI cut its repo rate in a move economists said would support GDP growth and borrowing.", "/b/india-central-bank-cut"),
    ("world", 5, "Reserve Bank of India cuts rates as inflation eases",
     "India's central bank reduced its policy interest rate by a quarter point.", "/a/rbi-rate-cut"),
    # C: oil
    ("world", 12, "Oil prices jump after OPEC+ agrees deeper output cut",
     "Brent crude rose sharply after OPEC+ producers agreed to reduce supply, raising inflation risks.", "/a/opec-cut"),
    ("biz", 11, "Crude climbs as OPEC+ slashes production",
     "Oil prices climbed after the producer group announced lower output targets.", "/b/crude-opec"),
    ("india", 10, "Higher oil prices could widen India's import bill, economists warn",
     "OPEC+ output cut may raise India's oil import costs and pressure the rupee.", "/i/oil-import-bill"),
    # D: ceasefire talks
    ("world", 4, "Ceasefire talks resume in Geneva as Russia and Ukraine weigh proposal",
     "Diplomats said a draft ceasefire framework could be discussed at a summit next week.", "/a/ceasefire-geneva"),
    ("india", 3, "Russia, Ukraine envoys meet in Geneva for truce talks",
     "Officials from Russia and Ukraine began talks on a possible truce, with sanctions relief on the table.", "/i/truce-talks"),
    # E: soft news
    ("world", 2, "Champions crowned after dramatic football final in Madrid",
     "Fans celebrated as the tournament match ended in a penalty shootout.", "/a/football-final"),
    ("biz", 2, "Football final draws record television audience",
     "The match and tournament broke viewing records across Europe.", "/b/football-ratings"),
    # F: single-source minor item
    ("biz", 1, "Regional bakery chain opens flagship store downtown",
     "The family-owned business opened its largest location so far.", "/b/bakery-opening"),
    # G: EU tariffs on EVs
    ("world", 14, "EU imposes tariffs on Chinese electric vehicle imports",
     "The European Commission said the duties respond to subsidies; Beijing threatened retaliation.", "/a/eu-ev-tariffs"),
    ("biz", 13, "Brussels slaps duties on China-made EVs, Beijing vows response",
     "European Union tariffs on Chinese electric cars took effect, raising trade war fears.", "/b/eu-ev-duties"),
]

FEEDS = {
    "world": ("Example World Service", "https://world.example-news.test", "https://world.example-news.test/rss.xml", "GB"),
    "biz": ("Sample Business Daily", "https://biz.sample-daily.test", "https://biz.sample-daily.test/feed", "US"),
    "india": ("Demo India Chronicle", "https://www.demo-india.test", "https://www.demo-india.test/rss", "IN"),
}

# Duplicates that dedup must drop: same URL with tracking params, and same title from same publisher.
DUPLICATES = [
    ("world", 10, "United States announces new semiconductor export controls on China",
     "Duplicate via tracking params.", "/a/us-chip-controls?utm_source=rss&utm_medium=feed"),
    ("india", 6, "RBI cuts repo rate by 25 basis points as inflation cools",
     "Same headline republished at a second URL.", "/i/rbi-cuts-repo-2"),
]


def rss_xml(feed_key: str, now: datetime = NOW, include_duplicates: bool = True) -> str:
    name, base, _, _ = FEEDS[feed_key]
    rows = [s for s in STORIES if s[0] == feed_key]
    if include_duplicates:
        rows += [d for d in DUPLICATES if d[0] == feed_key]
    items = []
    for _, hours, title, desc, path in rows:
        pub = format_datetime(now - timedelta(hours=hours))
        items.append(f"<item><title>{escape(title)}</title><link>{escape(base + path)}</link>"
                     f"<description>{escape('<p>' + desc + '</p>')}</description><pubDate>{pub}</pubDate></item>")
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(name)} (SYNTHETIC)</title>'
            f"<link>{base}</link><description>Synthetic test feed</description>{''.join(items)}</channel></rss>")


def feeds_yaml() -> str:
    lines = ["feeds:"]
    for key, (name, base, url, country) in FEEDS.items():
        domain = base.split("//", 1)[1].removeprefix("www.")
        lines.append(f"  - {{name: '{name}', publisher: '{name}', domain: {domain}, url: '{url}', country: {country}, "
                     f"source_type: news, enabled: true, verified: false}}")
    return "\n".join(lines) + "\n"


def gdelt_json(now: datetime = NOW) -> str:
    seen = (now - timedelta(hours=3)).strftime("%Y%m%dT%H%M%SZ")
    return json.dumps({"articles": [
        {"url": "https://gdelt-sample.test/2026/chip-export-rules", "url_mobile": "",
         "title": "US expands chip export rules targeting China semiconductor firms", "seendate": seen,
         "socialimage": "", "domain": "gdelt-sample.test", "language": "English", "sourcecountry": "Canada"},
        {"url": "https://other-sample.test/opec-supply", "url_mobile": "",
         "title": "OPEC+ output cut sends oil prices higher", "seendate": seen,
         "socialimage": "", "domain": "other-sample.test", "language": "English", "sourcecountry": "Australia"},
        {"url": "not-a-url", "title": "bad row"},
        "garbage-row",
    ]})


GDELT_RATE_LIMIT_TEXT = "Please limit requests to one every 5 seconds or contact us for larger queries."


def newsdata_json(now: datetime = NOW, next_page: str | None = None) -> dict:
    """SYNTHETIC NewsData.io /latest response (fictional sources), including the
    paid-plan placeholder strings the free tier returns."""
    pub = (now - timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "status": "success", "totalResults": 3, "nextPage": next_page,
        "results": [
            {"article_id": "a" * 32, "title": "India and EU conclude round of free trade agreement talks",
             "link": "https://nd-sample.test/india-eu-fta", "keywords": None, "creator": ["Staff Writer"],
             "description": "Negotiators said tariff schedules for autos and wine remain open.",
             "content": "ONLY AVAILABLE IN PAID PLANS", "pubDate": pub, "pubDateTZ": "UTC",
             "image_url": None, "video_url": None, "source_id": "ndsample", "source_name": "ND Sample Times",
             "source_priority": 1200, "source_url": "https://www.nd-sample.test", "source_icon": None,
             "language": "english", "country": ["india"], "category": ["business", "politics"],
             "sentiment": "ONLY AVAILABLE IN PROFESSIONAL AND CORPORATE PLANS",
             "ai_tag": "ONLY AVAILABLE IN PROFESSIONAL AND CORPORATE PLANS",
             "ai_region": "ONLY AVAILABLE IN CORPORATE PLANS", "ai_summary": "ONLY AVAILABLE IN PAID PLANS",
             "duplicate": False},
            {"article_id": "b" * 32, "title": "Duplicate copy flagged by provider", "link": "https://nd-sample.test/dup",
             "description": None, "pubDate": pub, "pubDateTZ": "UTC", "source_name": "ND Sample Times",
             "source_url": "https://www.nd-sample.test", "language": "english", "country": ["india"],
             "category": ["top"], "duplicate": True},
            {"article_id": "c" * 32, "title": "Rupee steadies as RBI signals liquidity support",
             "link": "https://other-nd.test/rupee", "description": "ONLY AVAILABLE IN PAID PLANS",
             "pubDate": pub, "pubDateTZ": "Asia/Kolkata", "source_name": None, "source_id": "othernd",
             "source_url": "https://other-nd.test", "language": "english", "country": ["united states of america"],
             "category": None, "creator": None, "duplicate": False},
        ],
    }
