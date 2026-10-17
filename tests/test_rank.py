from datetime import timedelta

from test_cluster import cluster, group_of, synthetic_articles

from fixtures import synthetic_news as sn
from intelpod.config import RankingConfig, RankingWeights
from intelpod.pipeline.normalize import normalize_item
from intelpod.pipeline.rank import Ranker
from intelpod.schemas import RawItem


def ranked(taxonomy, cfg=None, arts=None):
    arts = arts or synthetic_articles()
    cands = cluster(arts, taxonomy)
    return Ranker(cfg or RankingConfig(), taxonomy).rank(cands, {a.article_id: a for a in arts}), arts, cands


def find(rs, fragment):
    return next(r for r in rs if fragment.lower() in r.title.lower())


def test_hard_news_selected_and_soft_news_excluded(taxonomy):
    rs, _, _ = ranked(taxonomy)
    selected = [r for r in rs if r.selected]
    assert 3 <= len(selected) <= 8
    assert [r.rank for r in selected] == list(range(1, len(selected) + 1))
    titles = " ".join(r.title.lower() for r in selected)
    assert "football" not in titles and "bakery" not in titles
    football = find(rs, "football")
    assert "soft_news" in football.flags and not football.selected
    assert all(r.rationale for r in rs)


def test_every_score_is_explained(taxonomy):
    rs, _, _ = ranked(taxonomy)
    chip = find(rs, "semiconductor")
    names = {c.name for c in chip.components}
    assert names == {"geopolitical", "economic", "business", "india", "novelty", "long_term", "corroboration", "uncertainty"}
    assert all(c.evidence for c in chip.components)
    assert chip.selected and "Selected #" in chip.rationale and "Main drivers" in chip.rationale


def test_single_source_penalised(taxonomy):
    rs, _, _ = ranked(taxonomy)
    bakery = find(rs, "bakery")
    assert "single_source" in bakery.flags and not bakery.selected


def test_duplicate_volume_does_not_inflate_corroboration(taxonomy):
    """Ten syndicated copies of one headline must not out-score genuinely independent coverage."""
    def a(title, i, pub):
        return normalize_item(RawItem(provider="rss", title=title, url=f"https://p{i}.test/x", source_name=pub,
                                      published_at=sn.NOW - timedelta(hours=1)), now=sn.NOW)
    from intelpod.pipeline.dedup import deduplicate
    copies = [a("Government announces tariff increase on imported steel products from abroad", i, f"Outlet {i}")
              for i in range(10)]
    arts, _ = deduplicate(copies)
    rs, _, _ = ranked(taxonomy, arts=arts)
    corr = next(c for c in rs[0].components if c.name == "corroboration")
    assert rs[0].independent_sources == 1 and rs[0].article_count == 10
    assert corr.value < 0.4


def test_weights_are_configurable(taxonomy):
    base, _, _ = ranked(taxonomy)
    heavy_india = RankingConfig(weights=RankingWeights(india=10.0))
    rs, _, _ = ranked(taxonomy, heavy_india)
    rbi_base, rbi_india = find(base, "rate"), find(rs, "rate")
    chip_base, chip_india = find(base, "semiconductor"), find(rs, "semiconductor")
    assert rbi_india.importance - rbi_base.importance > chip_india.importance - chip_base.importance


def test_quiet_day_returns_fewer_events(taxonomy):
    cfg = RankingConfig(min_score=95, quiet_day_floor=90)
    rs, _, _ = ranked(taxonomy, cfg)
    assert not any(r.selected for r in rs)
    assert all("below threshold" in r.rationale for r in rs if "soft_news" not in r.flags)


def test_quiet_day_fill_flagged(taxonomy):
    base, _, _ = ranked(taxonomy)
    second_best = sorted(r.importance for r in base if r.selected)[-2]
    cfg = RankingConfig(min_score=second_best + 0.1, quiet_day_floor=0, min_events=3)
    rs, _, _ = ranked(taxonomy, cfg)
    sel = [r for r in rs if r.selected]
    assert len(sel) == 3 and sum("quiet_day_fill" in r.flags for r in sel) == 2


def test_topic_cap(taxonomy):
    cfg = RankingConfig(max_per_topic=1, min_score=0, quiet_day_floor=0)
    rs, _, _ = ranked(taxonomy, cfg)
    topics = [r.topic for r in rs if r.selected]
    assert len(topics) == len(set(topics))


def test_trade_war_is_not_read_as_armed_conflict(taxonomy):
    assert taxonomy.significance_hits("geopolitical", "Tariffs spark fears of a trade war") == []
    assert taxonomy.significance_hits("geopolitical", "War erupts on the border") == ["war"]
    assert "geopolitics" not in taxonomy.topic_hits("Price war hits airline stocks")


def test_evidence_reflects_topic_coverage(taxonomy):
    rs, _, _ = ranked(taxonomy)
    chip = find(rs, "semiconductor")
    econ = next(c for c in chip.components if c.name == "economic")
    assert any("reports on" in e for e in econ.evidence)
    geo = next(c for c in chip.components if c.name == "geopolitical")
    assert "export controls" in geo.evidence[0]


def test_incidental_country_mention_not_listed(taxonomy):
    _, arts, cands = ranked(taxonomy)
    oil = group_of(cands, arts, "OPEC+ agrees")
    assert "India" not in oil.countries


def test_briefing_skips_a_second_fragment_of_the_same_story(taxonomy):
    """User report (Oct 2026): 3 of 8 briefing slots covered one story split across events."""
    arts = synthetic_articles()
    cands = cluster(arts, taxonomy)
    chip = group_of(cands, arts, "semiconductor export controls")
    by_id = {a.article_id: a for a in arts}
    half = len(chip.article_ids) // 2
    first = chip.model_copy(update={"article_ids": chip.article_ids[:half]})
    second = chip.model_copy(update={"event_id": "ev_fragment", "article_ids": chip.article_ids[half:]})
    others = [c for c in cands if c.event_id != chip.event_id]
    rs = Ranker(RankingConfig(), taxonomy).rank(others + [first, second], by_id)
    picked = [r for r in rs if r.event_id in (first.event_id, second.event_id)]
    assert sum(r.selected for r in picked) == 1
    skipped = next(r for r in picked if not r.selected)
    assert "probably the same story as the selected event" in skipped.rationale
    rs = Ranker(RankingConfig(duplicate_similarity=0), taxonomy).rank(others + [first, second], by_id)
    assert all(r.selected for r in rs if r.event_id in (first.event_id, second.event_id))


def test_same_countries_different_story_is_not_a_duplicate(taxonomy):
    from intelpod.pipeline.rank import event_similarity
    arts = synthetic_articles()
    cands = cluster(arts, taxonomy)
    chip = group_of(cands, arts, "semiconductor export controls")
    ev = group_of(cands, arts, "electric vehicle")  # both China stories
    assert event_similarity(chip, ev) < RankingConfig().duplicate_similarity
