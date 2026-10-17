"""Clustering quality on a labelled corpus of realistic (synthetic) headlines.

tests/fixtures/clustering_corpus.json holds five "days" of model-written headlines from
fictional outlets: per day two big stories (20+ reports in many wordings), four medium
ones, distractors that share countries and topic words with a story but are different
events, and one-off stories. It is evaluation data only, never news.

Run `python -m tests.test_cluster_corpus` to print the metrics, e.g. after changing
clustering settings.
"""
from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from intelpod.config import PROJECT_ROOT, ClusteringConfig, RankingConfig
from intelpod.pipeline.cluster import Clusterer, build_candidates, build_docs
from intelpod.pipeline.dedup import deduplicate
from intelpod.pipeline.normalize import normalize_item
from intelpod.pipeline.rank import Ranker
from intelpod.schemas import RawItem
from intelpod.textutil import load_taxonomy, repair_gdelt_title

CORPUS = Path(__file__).parent / "fixtures" / "clustering_corpus.json"
NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
TAX = load_taxonomy(PROJECT_ROOT / "config" / "taxonomy.yaml")


def _gdelt_style(title: str) -> str:
    """GDELT returns titles with spaced punctuation ("U . S . - led"); the provider repairs them."""
    t = re.sub(r"([.,:;!?%])", r" \1 ", title)
    t = re.sub(r"(\w)-(\w)", r"\1 - \2", t)
    t = re.sub(r"(\w)'(\w)", r"\1 ' \2", t)
    return re.sub(r"\s+", " ", t).strip()


def load_corpus(gdelt_share: float = 0.35, seed: int = 7):
    """Articles plus gold labels. A share of title-only items is passed through the GDELT
    title round trip, as in a real run that mixes RSS and GDELT."""
    rnd = random.Random(seed)
    items, labels, kinds = [], {}, {}
    for day in json.loads(CORPUS.read_text())["days"]:
        for st in day["stories"]:
            sid = f"{day['day']}:{st['story_id']}"
            kinds[sid] = st["kind"]
            for i, h in enumerate(st["headlines"]):
                slug = re.sub("[^a-z]", "", h["publisher"].lower())[:20] or "pub"
                url = f"https://{slug}.test/{day['day']}/{st['story_id']}/{i}"
                title, provider, date_source = h["title"], "rss", "feed"
                if not h["description"] and rnd.random() < gdelt_share:
                    title, provider, date_source = repair_gdelt_title(_gdelt_style(title)), "gdelt", "gdelt_seendate"
                items.append(RawItem(provider=provider, title=title, url=url, description=h["description"] or None,
                                     source_name=h["publisher"], date_source=date_source,
                                     published_at=NOW - timedelta(hours=float(h["hours_ago"]))))
                labels[url] = sid
    arts = [a for a in (normalize_item(x, now=NOW) for x in items) if a]
    arts, _ = deduplicate(arts)
    return arts, {a.article_id: labels[a.source_url] for a in arts}, kinds


def cluster_metrics(cfg: ClusteringConfig, corpus=None) -> dict:
    """B-cubed precision/recall/F1 and how many events each big story is split into."""
    arts, gold, kinds = corpus or load_corpus()
    cands = build_candidates(Clusterer(cfg).cluster(build_docs(arts, TAX, cfg=cfg)), TAX, cfg)
    pred = {a: i for i, c in enumerate(cands) for a in c.article_ids}
    by_pred, by_gold = defaultdict(list), defaultdict(list)
    for a, p in pred.items():
        by_pred[p].append(a)
        by_gold[gold[a]].append(a)
    precision = recall = 0.0
    for a in pred:
        correct = sum(1 for b in by_pred[pred[a]] if gold[b] == gold[a])
        precision += correct / len(by_pred[pred[a]])
        recall += correct / len(by_gold[gold[a]])
    precision, recall = precision / len(pred), recall / len(pred)
    big = [Counter(pred[a] for a in members) for sid, members in by_gold.items() if kinds[sid] == "big"]
    return {"precision": round(precision, 3), "recall": round(recall, 3),
            "f1": round(2 * precision * recall / (precision + recall), 3),
            "big_story_events": round(sum(len(c) for c in big) / len(big), 2),
            "big_story_largest_share": round(sum(c.most_common(1)[0][1] / sum(c.values()) for c in big) / len(big), 3)}


def briefing_duplicates(cfg: ClusteringConfig, ranking: RankingConfig, corpus=None) -> int:
    """Selected briefing slots, summed over the five days, that repeat a story already selected."""
    arts, gold, _ = corpus or load_corpus()
    by_id = {a.article_id: a for a in arts}
    dups = 0
    for day in sorted({sid.split(":")[0] for sid in gold.values()}):
        sub = [a for a in arts if gold[a.article_id].startswith(day + ":")]
        cands = build_candidates(Clusterer(cfg).cluster(build_docs(sub, TAX, cfg=cfg)), TAX, cfg)
        events = {c.event_id: c for c in cands}
        stories = [Counter(gold[a] for a in events[r.event_id].article_ids).most_common(1)[0][0]
                   for r in Ranker(ranking, TAX).rank(cands, by_id) if r.selected]
        dups += len(stories) - len(set(stories))
    return dups


def test_default_clustering_quality_on_labelled_corpus():
    corpus = load_corpus()
    new = cluster_metrics(ClusteringConfig(), corpus)
    old = cluster_metrics(ClusteringConfig(country_tokens="drop"), corpus)
    # Measured Oct 2026: default f1 0.729 (precision 0.827), big stories in 7.9 events;
    # "drop" (previous default) f1 0.641, 11.3 events per big story.
    assert new["precision"] >= 0.80, new
    assert new["f1"] >= 0.70, new
    assert new["big_story_events"] <= 9.0, new
    assert new["f1"] >= old["f1"] + 0.05, (new, old)


def test_briefing_does_not_repeat_stories_much():
    corpus = load_corpus()
    with_guard = briefing_duplicates(ClusteringConfig(), RankingConfig(), corpus)
    without = briefing_duplicates(ClusteringConfig(), RankingConfig(duplicate_similarity=0), corpus)
    assert with_guard <= without and with_guard <= 6, (with_guard, without)


if __name__ == "__main__":
    overrides = dict(kv.split("=", 1) for kv in sys.argv[1:])
    cfg = ClusteringConfig.model_validate({**ClusteringConfig().model_dump(),
                                           **{k: json.loads(v) if v[:1].isdigit() else v for k, v in overrides.items()}})
    print(cluster_metrics(cfg))
    print("briefing duplicate slots:", briefing_duplicates(cfg, RankingConfig()))
