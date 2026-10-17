"""Clustering quality regression on PLANTED synthetic events (not real news).

Generates many events (each = a few rare key terms + generic news words + a country) and
one-off "noise" stories, then checks that clusters stay pure (unrelated stories are not
absorbed) and complete (an event's reports end up together). Guards against the
"black hole" failure where a large, diffuse cluster swallows everything.
"""
import random, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from intelpod.config import ClusteringConfig, PROJECT_ROOT
from intelpod.pipeline.cluster import Clusterer, build_docs, build_candidates
from intelpod.pipeline.normalize import normalize_item
from intelpod.schemas import RawItem
from intelpod.textutil import load_taxonomy

def run(seed=1, n_events=60, per_event=(2, 12), singletons=400, cfg=None, quiet=False):
    random.seed(seed)
    t = load_taxonomy(PROJECT_ROOT/"config/taxonomy.yaml")
    syll = "ka lo mi ra zu te vo ni sha pe du go li ba re fo xi mu ta zo".split()
    rare = sorted({"".join(random.sample(syll, 3)) for _ in range(5000)})  # sorted: set order varies per process
    generic = ("government officials minister plan talks deal prices market growth report data workers company "
               "state national leaders policy crisis rise fall record high low support new deal economy global "
               "trade energy security court law vote protest police attack military bank rates oil inflation").split()
    places = ["China","India","Russia","Brazil","Germany","Japan","Iran","Israel","Canada","Mexico","Kenya","Turkey","France","Ukraine"]
    now = datetime(2026,10,17,tzinfo=timezone.utc)
    items, truth = [], {}
    for e in range(n_events):
        keys = random.sample(rare, 4); gen = random.sample(generic, 3); place = random.choice(places)
        for j in range(random.randint(*per_event)):
            words = random.sample(keys, random.choice([2,3])) + random.sample(gen, 2) + random.sample(generic, 2)
            random.shuffle(words)
            title = (place + " " if random.random() < .7 else "") + " ".join(words)
            url = f"https://p{j}.test/e{e}-{j}"
            items.append(RawItem(provider="rss", title=title, url=url, source_name=f"Pub {j}", published_at=now-timedelta(hours=random.randint(0,30))))
            truth[url] = e
    for s in range(singletons):
        words = random.sample(generic, 4) + random.sample(rare, 2)
        url = f"https://s.test/{s}"
        items.append(RawItem(provider="rss", title=random.choice(places)+" "+" ".join(words), url=url, source_name=f"Pub {s%30}", published_at=now-timedelta(hours=random.randint(0,30))))
        truth[url] = f"s{s}"
    arts = [normalize_item(x, now=now) for x in items]
    by_id = {a.article_id: truth[a.source_url] for a in arts}
    cfg = cfg or ClusteringConfig()
    t0 = time.time()
    cands = build_candidates(Clusterer(cfg).cluster(build_docs(arts, t, cfg=cfg)), t, cfg)
    dt = time.time() - t0
    # purity: for each cluster, majority truth label share; completeness: each planted event's largest share in one cluster
    pure = sum(Counter(by_id[a] for a in c.article_ids).most_common(1)[0][1] for c in cands) / len(arts)
    ev_clusters = defaultdict(Counter)
    for i, c in enumerate(cands):
        for a in c.article_ids:
            if isinstance(by_id[a], int): ev_clusters[by_id[a]][i] += 1
    complete = sum(max(v.values()) for v in ev_clusters.values()) / sum(sum(v.values()) for v in ev_clusters.values())
    biggest = max(len(c.article_ids) for c in cands)
    if not quiet:
        print(f"articles={len(arts)} clusters={len(cands)} purity={pure:.3f} completeness={complete:.3f} biggest={biggest} time={dt:.1f}s")
    return pure, complete, biggest

def test_planted_event_purity_and_completeness():
    for seed in (1, 2):
        purity, completeness, biggest = run(seed=seed, n_events=40, singletons=250, quiet=True)
        assert purity >= 0.85, purity
        assert completeness >= 0.95, completeness
        assert biggest <= 30, biggest


def _high_floor_embedder_factory(floor_strength):
    """Fake embedding model whose unrelated texts still score ~0.7-0.9 cosine (like many real models)."""
    import math
    from intelpod.pipeline import cluster as C
    real_build = C.build_docs

    def build(arts, tax, embedder=None, cfg=None):
        docs = real_build(arts, tax, cfg=cfg)
        for d in docs:
            v = [0.0] * 64
            for t, w in d.vec.items():
                rng = random.Random(t)
                for k in range(64):
                    v[k] += w * rng.gauss(0, 1)
            n = math.sqrt(sum(x * x for x in v)) or 1
            v = [x / n for x in v]
            v[0] += floor_strength
            n = math.sqrt(sum(x * x for x in v))
            d.dense = [x / n for x in v]
        return docs
    return build


def test_high_floor_embeddings_do_not_collapse_clusters(monkeypatch):
    """Without calibration, an embedding model with a high cosine floor merged every article
    into one cluster. Calibrated average-linkage blending must keep (or improve) purity."""
    import sys
    from intelpod.config import EmbeddingsConfig
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "build_docs", _high_floor_embedder_factory(1.8))
    for mode in ("boost", "blend"):
        cfg = ClusteringConfig(embeddings=EmbeddingsConfig(enabled=True, weight=0.5, mode=mode))
        purity, completeness, biggest = run(seed=1, n_events=40, singletons=250, cfg=cfg, quiet=True)
        # No collapse in either mode. "blend" may also split events a little (that is why
        # "boost" is the default), so its completeness bar is lower.
        min_completeness = 0.95 if mode == "boost" else 0.90
        assert purity >= 0.85 and completeness >= min_completeness and biggest <= 30, (mode, purity, completeness, biggest)


def test_uninformative_embeddings_never_split_events_in_boost_mode(monkeypatch):
    """A bad embedding model (pure noise) must not make boost-mode clustering worse than lexical."""
    import math
    import sys
    from intelpod.config import EmbeddingsConfig
    from intelpod.pipeline import cluster as C
    real_build = C.build_docs

    def noisy_build(arts, tax, embedder=None, cfg=None):
        docs = real_build(arts, tax, cfg=cfg)
        for d in docs:
            rng = random.Random(d.article.article_id)
            v = [rng.gauss(0, 1) * 0.5 for _ in range(48)]
            v[0] += 1.8
            n = math.sqrt(sum(x * x for x in v))
            d.dense = [x / n for x in v]
        return docs

    base = run(seed=2, n_events=40, singletons=250, quiet=True)
    monkeypatch.setattr(sys.modules[__name__], "build_docs", noisy_build)
    boosted = run(seed=2, n_events=40, singletons=250, quiet=True,
                  cfg=ClusteringConfig(embeddings=EmbeddingsConfig(enabled=True, mode="boost")))
    assert boosted[1] >= base[1] - 0.005 and boosted[0] >= base[0] - 0.02, (base, boosted)


if __name__ == "__main__":
    run()
