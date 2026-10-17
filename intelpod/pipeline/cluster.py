"""Group articles into events.

Algorithm (deterministic, no model required):
  * Each article -> TF-IDF vector over title (weighted x2) + description, after
    synonym folding ("semiconductor" -> "chip", "curbs" -> "restrict") and light stemming.
  * Each article -> entity set: canonical countries (Washington/American -> United
    States) plus capitalised proper-noun phrases.
  * Similarity = cosine + ew*entity_overlap*gate (capped at 1). Shared entities are a
    bonus, never a penalty; the gate scales that bonus by lexical agreement so two
    unrelated "United States" stories do not merge on the country name alone.
    With Ollama embeddings enabled, cosine is a blend of lexical and embedding cosine.
  * Greedy single pass in time order (article joins the best cluster above threshold),
    then iterative centroid merge for clusters above `merge_threshold`.
  * Clusters are then linked to events from earlier runs so ongoing stories keep a
    stable event_id.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable

from ..config import ClusteringConfig
from ..schemas import Article, EventCandidate
from ..textutil import Taxonomy, country_alias_tokens, find_countries, find_named_entities, stable_hash, tokenize
from .embeddings import dense_cos

Vector = dict[str, float]
ENTITY_GATE_COS = 0.20  # lexical cosine at which entity overlap gets full credit


@dataclass
class Doc:
    article: Article
    vec: Vector
    entities: frozenset[str]
    countries: list[str]
    dense: list[float] | None = None

    @property
    def time(self) -> datetime:
        return self.article.effective_time


@dataclass
class Cluster:
    members: list[Doc] = field(default_factory=list)
    sum_vec: Vector = field(default_factory=dict)
    sum_dense: list[float] | None = None
    ent_counts: Counter = field(default_factory=Counter)
    first: datetime | None = None
    last: datetime | None = None

    def add(self, doc: Doc) -> None:
        self.members.append(doc)
        for t, w in doc.vec.items():
            self.sum_vec[t] = self.sum_vec.get(t, 0.0) + w
        if doc.dense is not None:
            self.sum_dense = list(doc.dense) if self.sum_dense is None else [a + b for a, b in zip(self.sum_dense, doc.dense)]
        self.ent_counts.update(doc.entities)
        self.first = doc.time if self.first is None else min(self.first, doc.time)
        self.last = doc.time if self.last is None else max(self.last, doc.time)

    def absorb(self, other: "Cluster") -> None:
        for d in other.members:
            self.add(d)

    def centroid(self) -> Vector:
        return _normalize(self.sum_vec)

    def dense_centroid(self) -> list[float] | None:
        if self.sum_dense is None:
            return None
        n = math.sqrt(sum(x * x for x in self.sum_dense)) or 1.0
        return [x / n for x in self.sum_dense]

    def entity_share(self) -> dict[str, float]:
        n = len(self.members)
        return {e: c / n for e, c in self.ent_counts.items()}


def _normalize(v: Vector) -> Vector:
    n = math.sqrt(sum(w * w for w in v.values())) or 1.0
    return {t: w / n for t, w in v.items()}


def cosine(a: Vector, b: Vector) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(w * b.get(t, 0.0) for t, w in a.items())


def article_text(a: Article) -> str:
    return f"{a.title}. {a.description or ''}"


def build_docs(articles: list[Article], taxonomy: Taxonomy,
               embedder: Callable[[list[str]], list[list[float]] | None] | None = None) -> list[Doc]:
    exclude = country_alias_tokens()
    token_lists = [tokenize(taxonomy.fold_phrases(f"{a.title} {a.title} {a.description or ''}"), taxonomy.synonyms, exclude)
                   for a in articles]
    df: Counter = Counter()
    for toks in token_lists:
        df.update(set(toks))
    n = len(articles)
    docs = []
    for a, toks in zip(articles, token_lists):
        tf = Counter(toks)
        vec = {t: (1 + math.log(c)) * (math.log((1 + n) / (1 + df[t])) + 1) for t, c in tf.items()}
        text = article_text(a)
        countries = find_countries(text)
        ents = {f"country:{c.lower()}" for c in countries}
        ents |= {f"ent:{e}" for e in find_named_entities(a.title)}
        docs.append(Doc(article=a, vec=_normalize(vec), entities=frozenset(ents), countries=countries))
    if embedder and docs:
        dense = embedder([article_text(d.article) for d in docs])
        if dense:
            for d, v in zip(docs, dense):
                d.dense = v
    return docs


class Clusterer:
    def __init__(self, cfg: ClusteringConfig):
        self.cfg = cfg
        self.max_gap = timedelta(hours=cfg.max_time_gap_hours)

    def _lexical(self, cos: float, dense_cos_value: float | None) -> float:
        if dense_cos_value is None:
            return cos
        w = self.cfg.embeddings.weight
        return w * dense_cos_value + (1 - w) * cos

    def _pair_lex(self, a: Doc, b: Doc) -> float:
        return self._lexical(cosine(a.vec, b.vec), dense_cos(a.dense, b.dense) if a.dense and b.dense else None)

    def has_direct_match(self, doc: Doc, cl: Cluster) -> bool:
        floor = self.cfg.member_min_similarity
        return any(self._pair_lex(doc, m) >= floor for m in cl.members)

    def doc_to_cluster(self, doc: Doc, cl: Cluster) -> float:
        if not self.has_direct_match(doc, cl):
            return 0.0
        cos = cosine(doc.vec, cl.centroid())
        dc = cl.dense_centroid()
        lex = self._lexical(cos, dense_cos(doc.dense, dc) if doc.dense and dc else None)
        share = cl.entity_share()
        ent = sum(share.get(e, 0.0) for e in doc.entities) / max(1, len(doc.entities))
        return self._combine(lex, ent)

    def cluster_to_cluster(self, a: Cluster, b: Cluster) -> float:
        cos = cosine(a.centroid(), b.centroid())
        da, db = a.dense_centroid(), b.dense_centroid()
        lex = self._lexical(cos, dense_cos(da, db) if da and db else None)
        ea, eb = a.entity_share(), b.entity_share()
        keys = set(ea) | set(eb)
        ent = sum(min(ea.get(k, 0), eb.get(k, 0)) for k in keys) / max(1e-9, sum(max(ea.get(k, 0), eb.get(k, 0)) for k in keys)) if keys else 0.0
        return self._combine(lex, ent)

    def _combine(self, lex: float, ent: float) -> float:
        return combine_similarity(lex, ent, self.cfg.entity_weight)

    def cluster(self, docs: list[Doc]) -> list[Cluster]:
        clusters: list[Cluster] = []
        index: dict[str, set[int]] = defaultdict(set)  # term -> cluster indexes
        for doc in sorted(docs, key=lambda d: (d.time, d.article.article_id)):
            candidates: set[int] = set()
            for t in doc.vec:
                candidates |= index.get(t, set())
            best, best_sim = None, 0.0
            for ci in candidates:
                cl = clusters[ci]
                if cl.last and abs(doc.time - cl.last) > self.max_gap and abs(doc.time - cl.first) > self.max_gap:
                    continue
                sim = self.doc_to_cluster(doc, cl)
                if sim > best_sim:
                    best, best_sim = ci, sim
            if best is not None and best_sim >= self.cfg.similarity_threshold:
                clusters[best].add(doc)
                target = best
            else:
                clusters.append(Cluster())
                clusters[-1].add(doc)
                target = len(clusters) - 1
            for t in doc.vec:
                index[t].add(target)
        return self.merge(clusters)

    def merge(self, clusters: list[Cluster], max_rounds: int = 10) -> list[Cluster]:
        for _ in range(max_rounds):
            merged_any = False
            alive = [c for c in clusters if c.members]
            alive.sort(key=lambda c: -len(c.members))
            for i, a in enumerate(alive):
                if not a.members:
                    continue
                for b in alive[i + 1:]:
                    if not b.members:
                        continue
                    if b.first - a.last > self.max_gap or a.first - b.last > self.max_gap:
                        continue
                    if self.cluster_to_cluster(a, b) >= self.cfg.merge_threshold and \
                            any(self.has_direct_match(d, a) for d in b.members):
                        a.absorb(b)
                        b.members = []
                        merged_any = True
            clusters = [c for c in alive if c.members]
            if not merged_any:
                break
        return clusters


def combine_similarity(lex: float, ent: float, entity_weight: float) -> float:
    gate = min(1.0, lex / ENTITY_GATE_COS)
    return min(1.0, lex + entity_weight * ent * gate)


def cohesion(cl: Cluster) -> float | None:
    if len(cl.members) < 2:
        return None
    c = cl.centroid()
    return round(sum(cosine(d.vec, c) for d in cl.members) / len(cl.members), 3)


def independent_sources(articles: Iterable[Article]) -> int:
    """Count independent sources of reporting for one event.

    * A publisher with at least one headline of its own (not shared with another
      publisher) counts once, however many such articles it ran.
    * A headline carried by several publishers (wire/syndicated copy) counts once in
      total, however many outlets republished it.
    """
    by_group: dict[str, set[str]] = defaultdict(set)
    for a in articles:
        by_group[a.syndication_group].add(a.source_name.lower())
    own_reporting = {next(iter(pubs)) for pubs in by_group.values() if len(pubs) == 1}
    shared_copies = sum(1 for pubs in by_group.values() if len(pubs) > 1)
    return len(own_reporting) + shared_copies


@dataclass
class PriorEvent:
    event_id: str
    centroid: Vector
    entities: list[str]
    first_seen_at: datetime
    article_ids: set[str]


def summarize_cluster(cl: Cluster, taxonomy: Taxonomy, top_terms: int = 40) -> dict:
    members = [d.article for d in cl.members]
    topic_counts: Counter = Counter()
    term_counts: dict[str, Counter] = defaultdict(Counter)
    for a in members:
        for topic, terms in taxonomy.topic_hits(article_text(a)).items():
            topic_counts[topic] += 1
            term_counts[topic].update(terms)
    # Primary topic: most member articles, then most matched terms, then name (deterministic).
    topic = min(topic_counts, key=lambda t: (-topic_counts[t], -sum(term_counts[t].values()), t)) if topic_counts else "other"
    subtopic = term_counts[topic].most_common(1)[0][0] if topic in term_counts else None
    country_counts: Counter = Counter(c for d in cl.members for c in d.countries)
    min_share = max(1, math.ceil(0.4 * len(members)))  # country must feature in 40% of reports
    countries = [c for c, n in country_counts.most_common() if n >= min_share]
    ent_counts = Counter(e[4:] for d in cl.members for e in d.entities if e.startswith("ent:"))
    min_ent = 2 if len(members) >= 3 else 1
    entities = [e for e, n in ent_counts.most_common(10) if n >= min_ent]
    centroid = cl.centroid()
    # Most central report; small bonus for having a description or being a primary source.
    rep = max(cl.members, key=lambda d: (cosine(d.vec, centroid) + 0.1 * bool(d.article.description)
                                          + 0.05 * (d.article.source_type == "official"), -d.time.timestamp()))
    top = dict(sorted(centroid.items(), key=lambda kv: -kv[1])[:top_terms])
    description = rep.article.description or next((a.description for a in members if a.description), None)
    return {
        "topic": topic, "subtopic": subtopic, "topics": dict(topic_counts), "countries": countries,
        "entities": entities, "representative": rep.article, "description": description,
        "centroid": {t: round(w, 4) for t, w in top.items()},
    }


def build_candidates(clusters: list[Cluster], taxonomy: Taxonomy, cfg: ClusteringConfig,
                     prior_events: list[PriorEvent] | None = None) -> list[EventCandidate]:
    """Turn clusters into EventCandidates, reusing IDs of ongoing events from earlier runs."""
    prior_events = prior_events or []
    article_to_prior: dict[str, str] = {aid: p.event_id for p in prior_events for aid in p.article_ids}
    prior_by_id = {p.event_id: p for p in prior_events}
    summaries = [summarize_cluster(cl, taxonomy) for cl in clusters]

    # Score every (cluster, prior event) pair, then assign greedily one-to-one.
    pairs: list[tuple[float, int, str]] = []
    for i, (cl, summ) in enumerate(zip(clusters, summaries)):
        shared = Counter(article_to_prior[d.article.article_id] for d in cl.members
                         if d.article.article_id in article_to_prior)
        for eid, n in shared.items():
            pairs.append((1.0 + n / len(cl.members), i, eid))  # shared articles beat any centroid match
        ents = {f"country:{c.lower()}" for c in summ["countries"]} | {f"ent:{e}" for e in summ["entities"]}
        for p in prior_events:
            if p.event_id in shared:
                continue
            lex = cosine(summ["centroid"], p.centroid)
            pents = set(p.entities)
            ent = len(ents & pents) / len(ents | pents) if ents | pents else 0.0
            sim = combine_similarity(lex, ent, cfg.entity_weight)
            if sim >= cfg.link_threshold:
                pairs.append((sim, i, p.event_id))
    assigned: dict[int, str] = {}
    used: set[str] = set()
    for sim, i, eid in sorted(pairs, reverse=True):
        if i not in assigned and eid not in used:
            assigned[i] = eid
            used.add(eid)

    out = []
    for i, (cl, summ) in enumerate(zip(clusters, summaries)):
        members = sorted((d.article for d in cl.members), key=lambda a: a.effective_time)
        ids = [a.article_id for a in members]
        prior = prior_by_id.get(assigned.get(i, ""))
        if prior:
            event_id = prior.event_id
            new_count = sum(1 for a in ids if a not in prior.article_ids)
        else:
            event_id = f"evt_{members[0].effective_time:%Y%m%d}_{stable_hash(*sorted(ids[:3]), length=8)}"
            new_count = len(ids)
        rep: Article = summ["representative"]
        pubs = sorted({a.source_name for a in members})
        out.append(EventCandidate(
            event_id=event_id, title=rep.title, description=summ["description"], article_ids=ids,
            representative_article_id=rep.article_id, topic=summ["topic"], subtopic=summ["subtopic"],
            topics=summ["topics"], countries=summ["countries"], entities=summ["entities"], publishers=pubs,
            independent_sources=independent_sources(members),
            first_article_at=members[0].effective_time, latest_article_at=members[-1].effective_time,
            cluster_confidence=cohesion(cl), is_continuing=prior is not None,
            previous_first_seen_at=prior.first_seen_at if prior else None, new_article_count=new_count,
            centroid=summ["centroid"],
        ))
    return out
