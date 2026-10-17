from datetime import timedelta

from fixtures import synthetic_news as sn
from intelpod.config import ClusteringConfig
from intelpod.pipeline.cluster import Clusterer, PriorEvent, build_candidates, build_docs
from intelpod.pipeline.dedup import deduplicate
from intelpod.pipeline.normalize import normalize_item
from intelpod.schemas import RawItem


def synthetic_articles():
    arts = []
    for key, hours, title, desc, path in sn.STORIES:
        name, base, _, country = sn.FEEDS[key]
        arts.append(normalize_item(RawItem(provider="rss", title=title, url=base + path, description=desc,
                                           source_name=name, published_at=sn.NOW - timedelta(hours=hours),
                                           date_source="feed", country=country), now=sn.NOW))
    return deduplicate(arts)[0]


def cluster(arts, taxonomy, cfg=None):
    cfg = cfg or ClusteringConfig()
    return build_candidates(Clusterer(cfg).cluster(build_docs(arts, taxonomy)), taxonomy, cfg)


def group_of(cands, arts, title_fragment):
    aid = next(a.article_id for a in arts if title_fragment.lower() in a.title.lower())
    return next(c for c in cands if aid in c.article_ids)


def test_chip_example_three_phrasings_form_one_event(taxonomy):
    arts = synthetic_articles()
    cands = cluster(arts, taxonomy)
    chip = group_of(cands, arts, "semiconductor export controls on China")
    assert group_of(cands, arts, "Washington tightens") is chip
    assert group_of(cands, arts, "New American measures") is chip
    assert len(chip.article_ids) == 4
    assert chip.independent_sources == 3  # syndicated wire copy is not independent
    assert "United States" in chip.countries and "China" in chip.countries
    assert chip.topic in {"semiconductors_minerals", "trade_policy"}
    assert chip.cluster_confidence is not None and chip.cluster_confidence > 0.3


def test_distinct_events_stay_separate(taxonomy):
    arts = synthetic_articles()
    cands = cluster(arts, taxonomy)
    chip = group_of(cands, arts, "semiconductor export controls")
    rbi = group_of(cands, arts, "RBI cuts repo rate")
    oil = group_of(cands, arts, "OPEC+ agrees")
    ev = group_of(cands, arts, "electric vehicle")
    ceasefire = group_of(cands, arts, "Ceasefire talks")
    assert len({id(c) for c in (chip, rbi, oil, ev, ceasefire)}) == 5
    assert len(rbi.article_ids) == 3 and len(oil.article_ids) == 3
    assert len(ceasefire.article_ids) == 2 and len(ev.article_ids) == 2
    assert rbi.topic == "markets_monetary"


def test_shared_country_alone_does_not_merge(taxonomy):
    from intelpod.textutil import stable_hash
    def a(title):
        return normalize_item(RawItem(provider="rss", title=title, url=f"https://x.test/{stable_hash(title)}",
                                      source_name="P", published_at=sn.NOW), now=sn.NOW)
    cands = cluster([a("United States wildfire forces evacuations in California"),
                     a("United States space agency delays moon mission launch")], taxonomy)
    assert len(cands) == 2


def test_time_gap_prevents_merging_old_story(taxonomy):
    cfg = ClusteringConfig(max_time_gap_hours=24)
    def a(title, hours, url):
        return normalize_item(RawItem(provider="rss", title=title, url=url, source_name="P",
                                      published_at=sn.NOW - timedelta(hours=hours)), now=sn.NOW)
    cands = cluster([a("OPEC+ agrees deeper oil output cut", 100, "https://x.test/1"),
                     a("OPEC+ agrees deeper oil output cut again", 1, "https://x.test/2")], taxonomy, cfg)
    assert len(cands) == 2


def test_linking_to_prior_event_keeps_stable_id(taxonomy):
    arts = synthetic_articles()
    first = cluster(arts, taxonomy)
    oil = group_of(first, arts, "OPEC+ agrees")
    prior = [PriorEvent(event_id=oil.event_id, centroid=oil.centroid,
                        entities=[f"country:{c.lower()}" for c in oil.countries], first_seen_at=sn.NOW,
                        article_ids=set(oil.article_ids[:1]))]
    cfg = ClusteringConfig()
    second = build_candidates(Clusterer(cfg).cluster(build_docs(arts, taxonomy)), taxonomy, cfg, prior)
    oil2 = group_of(second, arts, "OPEC+ agrees")
    assert oil2.event_id == oil.event_id and oil2.is_continuing
    assert oil2.new_article_count == len(oil2.article_ids) - 1
    assert sum(c.event_id == oil.event_id for c in second) == 1


def test_embeddings_blend_is_used_when_available(taxonomy):
    arts = synthetic_articles()[:4]
    calls = []
    def fake_embedder(texts):
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]
    docs = build_docs(arts, taxonomy, fake_embedder)
    assert calls == [4] and all(d.dense == [1.0, 0.0] for d in docs)
    docs = build_docs(arts, taxonomy, lambda t: None)  # unavailable -> lexical only
    assert all(d.dense is None for d in docs)
