"""How closely a research result is about the event (0..1), with the matched terms as evidence.

Uses the clustering text pipeline (phrase and synonym folding, country tokens, stemming) and
TF-IDF over this event's material (its own reports plus every candidate result), so words that
all candidates share count little and the event's distinctive words count a lot. Named people,
organisations and programmes add a bonus scaled by lexical agreement, as in clustering. An
optional embedder can only raise a score (catching paraphrases), never lower it.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Callable

from ..config import ClusteringConfig
from ..pipeline.cluster import combine_similarity, cosine
from ..textutil import COUNTRY_TOKEN_PREFIX, Taxonomy, fold_countries, is_country_alias, tokenize
from .models import EventBrief

ENTITY_WEIGHT = 0.3
KEY_TERMS = 10          # the event's most distinctive terms (no countries) used as anchors
TOP_TERMS = 4           # one of these alone is enough
OFF_TOPIC_FACTOR = 0.4  # score multiplier for results that mention none of the event's anchors


class RelevanceModel:
    def __init__(self, brief: EventBrief, taxonomy: Taxonomy, cluster_cfg: ClusteringConfig,
                 corpus_texts: list[str], embedder: Callable[[list[str]], list[list[float]] | None] | None = None,
                 event_texts: list[str] | None = None):
        self.taxonomy = taxonomy
        self.country_w = cluster_cfg.country_token_weight
        docs = [self._tokens(t) for t in corpus_texts]
        self.n = max(1, len(docs))
        self.df: Counter = Counter()
        for d in docs:
            self.df.update(set(d))
        title = brief.headline or brief.title
        profile = [f"{title} {title} {brief.summary or brief.description or ''}"] + list(event_texts or [])
        acc: dict[str, float] = {}
        for text in profile:
            for t, w in self._vec(text).items():
                acc[t] = acc.get(t, 0.0) + w
        self.profile = _norm(acc)
        self.key_terms = {t for t, _ in sorted(self.profile.items(), key=lambda kv: -kv[1])
                          if not t.startswith(("pair:", COUNTRY_TOKEN_PREFIX)) and len(t) > 2}
        ranked = sorted(self.key_terms, key=lambda t: -self.profile[t])
        self.key_terms = set(ranked[:KEY_TERMS])
        self.top_terms = set(ranked[:TOP_TERMS])
        self.entities = [e for e in brief.entities if not is_country_alias(e)][:8]
        self.embedder = embedder
        self._event_dense = None
        if embedder is not None:
            try:
                vecs = embedder([profile[0]])
                self._event_dense = vecs[0] if vecs else None
            except Exception:  # noqa: BLE001 - embeddings are optional
                self._event_dense = None

    def _tokens(self, text: str) -> list[str]:
        return tokenize(fold_countries(self.taxonomy.fold_phrases(text or "")), self.taxonomy.synonyms)

    def _vec(self, text: str) -> dict[str, float]:
        tf = Counter(self._tokens(text))
        out = {}
        for t, c in tf.items():
            idf = math.log((1 + self.n) / (1 + self.df.get(t, 0))) + 1
            w = (1 + math.log(c)) * idf
            out[t] = w * (self.country_w if t.startswith(COUNTRY_TOKEN_PREFIX) else 1.0)
        return _norm(out)

    def score(self, title: str, text: str = "") -> tuple[float, str]:
        vec = self._vec(f"{title} {title} {text}")
        cos = cosine(vec, self.profile)
        low = f"{title} {text}".lower()
        hits = [e for e in self.entities if e.lower() in low]
        ent = len(hits) / len(self.entities) if self.entities else 0.0
        score = combine_similarity(cos, ent, ENTITY_WEIGHT)
        # Similarity alone lets a long unrelated article through on shared common words and a shared
        # country ("US" midterms vs a US sanctions story). A result must name one of the event's
        # actors, one of its four most distinctive terms, or two of its top ten, near the top.
        head = set(self._tokens(f"{title} {text[:1500]}"))
        anchored = (bool(hits) or bool(head & self.top_terms) or len(head & self.key_terms) >= 2
                    or not self.key_terms)
        if self._event_dense is not None:
            try:
                vecs = self.embedder([f"{title}. {text[:500]}"])
                if vecs and vecs[0]:
                    dense = _dense_cos(vecs[0], self._event_dense)
                    score = max(score, 0.5 * max(0.0, (dense - 0.5) / 0.5) + 0.5 * score)
            except Exception:  # noqa: BLE001
                pass
        shared = sorted((t for t in vec if t in self.profile and not t.startswith(("pair:", COUNTRY_TOKEN_PREFIX))),
                        key=lambda t: -self.profile[t])[:5]
        why = []
        if shared:
            why.append("shares terms: " + ", ".join(shared))
        if hits:
            why.append("names: " + ", ".join(hits[:4]))
        if not anchored:
            score *= OFF_TOPIC_FACTOR
            why.append("names none of the event's actors or distinctive terms (score reduced)")
        return round(min(1.0, score), 3), "; ".join(why) or "few shared terms"


def _norm(v: dict[str, float]) -> dict[str, float]:
    n = math.sqrt(sum(w * w for w in v.values())) or 1.0
    return {t: w / n for t, w in v.items()}


def _dense_cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)
