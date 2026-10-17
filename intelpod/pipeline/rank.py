"""Transparent, configurable event importance ranking.

Every event gets eight component scores in [0, 1], each with the evidence that
produced it (matched terms, publishers, countries). Importance is the weighted mean
x 100, then multiplied by explicit penalties. Article volume never counts directly:
only *independent* publishers (syndicated copies collapsed) feed corroboration,
and that saturates at `corroboration_cap`.
"""
from __future__ import annotations

import math
from collections import Counter

from ..config import RankingConfig
from ..schemas import EXACT_DATE_SOURCES, Article, EventCandidate, EventReview, RankedEvent, ScoreComponent
from ..textutil import Taxonomy
from .cluster import article_text

GEO_TOPICS = {"geopolitics", "defense", "elections_politics"}
ECON_TOPICS = {"macroeconomy", "trade_policy", "energy_commodities", "markets_monetary"}
BUSINESS_TOPICS = {"corporate", "technology_ai", "semiconductors_minerals", "supply_chain", "trade_policy"}
HARD_TOPICS = GEO_TOPICS | ECON_TOPICS | BUSINESS_TOPICS

LABELS = {
    "geopolitical": "geopolitical significance", "economic": "economic significance",
    "business": "business impact", "india": "India relevance", "novelty": "novelty",
    "long_term": "potential long-term consequences", "corroboration": "independent corroboration",
    "uncertainty": "unresolved risk / uncertainty",
}


def sat(x: float, half: float) -> float:
    """Saturating curve: 0 -> 0, `half` -> 0.5, large -> 1."""
    return 0.0 if x <= 0 else x / (x + half)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


class Ranker:
    def __init__(self, cfg: RankingConfig, taxonomy: Taxonomy):
        self.cfg = cfg
        self.tax = taxonomy

    def score(self, ev: EventCandidate, articles: list[Article], review: EventReview | None = None,
              review_weight: float = 0.5) -> RankedEvent:
        texts = [article_text(a) for a in articles]
        n = max(1, len(texts))
        member_topics = [set(self.tax.topic_hits(t)) for t in texts]

        def frac(topics: set[str]) -> float:
            return sum(1 for mt in member_topics if mt & topics) / n

        def coverage(topics: set[str]) -> str:
            hit = Counter(t for mt in member_topics for t in mt & topics)
            k = sum(1 for mt in member_topics if mt & topics)
            return f"{k}/{n} reports on {', '.join(sorted(hit)) or 'these topics'}" if k else f"0/{n} reports on these topics"

        def sig(dim: str) -> list[str]:
            c: Counter = Counter()
            for t in texts:
                c.update(set(self.tax.significance_hits(dim, t)))
            return [term for term, _ in c.most_common()]

        w = self.cfg.weights
        comps: list[ScoreComponent] = []

        geo_terms = sig("geopolitical")
        geo = 0.45 * frac(GEO_TOPICS) + 0.35 * sat(len(geo_terms), 1.5) + 0.20 * sat(max(0, len(ev.countries) - 1), 1.5)
        comps.append(ScoreComponent(name="geopolitical", value=round(_clamp(geo), 3), weight=w.geopolitical,
                                    evidence=[f"terms: {', '.join(geo_terms[:6])}" if geo_terms else "no high-impact geopolitical terms",
                                              coverage(GEO_TOPICS),
                                              f"countries: {', '.join(ev.countries[:6]) or 'none identified'}"]))

        econ_terms = sig("economic")
        econ = 0.5 * frac(ECON_TOPICS) + 0.5 * sat(len(econ_terms), 1.5)
        comps.append(ScoreComponent(name="economic", value=round(_clamp(econ), 3), weight=w.economic,
                                    evidence=[f"terms: {', '.join(econ_terms[:6])}" if econ_terms else "no high-impact macro/market terms",
                                              coverage(ECON_TOPICS)]))

        biz_terms = sig("business")
        biz = 0.5 * frac(BUSINESS_TOPICS) + 0.5 * sat(len(biz_terms), 1.5)
        comps.append(ScoreComponent(name="business", value=round(_clamp(biz), 3), weight=w.business,
                                    evidence=[f"terms: {', '.join(biz_terms[:6])}" if biz_terms else "no high-impact business terms",
                                              coverage(BUSINESS_TOPICS)]))

        india_hits: Counter = Counter()
        india_members = 0
        for t in texts:
            hits = set(self.tax.india_hits(t))
            india_members += bool(hits)
            india_hits.update(hits)
        indian_pubs = sorted({a.source_name for a in articles if (a.country or "").upper() in {"IN", "INDIA"}})
        india = 0.6 * (india_members / n) + 0.25 * sat(len(india_hits), 1.5) + (0.15 if "India" in ev.countries else 0)
        comps.append(ScoreComponent(name="india", value=round(_clamp(india), 3), weight=w.india, evidence=[
            f"{india_members}/{n} reports mention India-related terms" + (f": {', '.join(t for t, _ in india_hits.most_common(5))}" if india_hits else ""),
            f"Indian publishers covering it: {', '.join(indian_pubs) or 'none'}"]))

        if ev.is_continuing:
            share_new = ev.new_article_count / max(1, len(ev.article_ids))
            novelty = 0.3 + 0.7 * share_new
            nov_ev = [f"continuing story first seen {ev.previous_first_seen_at:%Y-%m-%d}" if ev.previous_first_seen_at else "continuing story",
                      f"{ev.new_article_count} new of {len(ev.article_ids)} reports since last run"]
        else:
            novelty, nov_ev = 1.0, ["not seen in earlier runs"]
        comps.append(ScoreComponent(name="novelty", value=round(_clamp(novelty), 3), weight=w.novelty, evidence=nov_ev))

        lt_terms = sig("long_term")
        official = sorted({a.source_name for a in articles if a.source_type in {"official", "international_org"}})
        lt = sat(len(lt_terms), 1.5) + (0.2 if official else 0)
        comps.append(ScoreComponent(name="long_term", value=round(_clamp(lt), 3), weight=w.long_term,
                                    evidence=[f"terms: {', '.join(lt_terms[:6])}" if lt_terms else "no structural/policy terms"]
                                    + ([f"primary source: {', '.join(official)}"] if official else [])))

        indep = ev.independent_sources
        corr = math.log1p(indep) / math.log1p(self.cfg.corroboration_cap)
        if official:
            corr = max(corr, 0.5)
        comps.append(ScoreComponent(name="corroboration", value=round(_clamp(corr), 3), weight=w.corroboration,
                                    evidence=[f"{indep} independent publisher(s) of {len(ev.publishers)} total; "
                                              f"{len(articles)} reports (syndicated copies counted once)",
                                              f"publishers: {', '.join(ev.publishers[:8])}"]))

        unc_terms = sig("uncertainty")
        unc = sat(len(unc_terms), 1.5) * max(geo, econ, biz)
        comps.append(ScoreComponent(name="uncertainty", value=round(_clamp(unc), 3), weight=w.uncertainty,
                                    evidence=[f"terms: {', '.join(unc_terms[:6])}" if unc_terms else "no risk/uncertainty language"]))

        if review is not None:
            # Blend the model's judgement with the keyword score; both stay visible as evidence.
            for c in comps:
                a = review.scores.get(c.name)
                if a is None:
                    continue
                keyword = c.value
                c.value = round(_clamp((1 - review_weight) * keyword + review_weight * a.score), 3)
                c.evidence.append(f"model {a.score:.2f} ({a.reason}); keyword score {keyword:.2f}")

        total_w = sum(c.weight for c in comps) or 1.0
        importance = 100 * sum(c.value * c.weight for c in comps) / total_w

        penalties, flags = [], []
        soft_members = sum(1 for t in texts if self.tax.soft_hits(t))
        if soft_members / n >= 0.5 and frac(HARD_TOPICS) < 0.5:
            importance *= self.cfg.soft_news_penalty
            penalties.append(f"soft news (x{self.cfg.soft_news_penalty}): {soft_members}/{n} reports match sports/entertainment/lifestyle terms")
            flags.append("soft_news")
        elif review is not None and review.soft_news:
            importance *= self.cfg.soft_news_penalty
            penalties.append(f"soft news (x{self.cfg.soft_news_penalty}): judged sports/entertainment/lifestyle by {review.model}")
            flags.append("soft_news")
        if review is not None and review.outlier_article_ids:
            flags.append("model_removed_outliers")
        if review is not None and review.rejected:
            flags.append("model_text_rejected")
        if indep <= 1 and not official:
            importance *= self.cfg.single_source_penalty
            penalties.append(f"single source (x{self.cfg.single_source_penalty}): not yet independently corroborated")
            flags.append("single_source")
        if ev.topic == "other":
            flags.append("off_taxonomy")
        if any(a.date_source not in EXACT_DATE_SOURCES for a in articles):
            flags.append("some_dates_approximate")

        title, headline = ev.title, None
        if review is not None and review.title and review.title.strip() != ev.title.strip():
            title, headline = review.title, ev.title
        return RankedEvent(
            rank=None, selected=False, event_id=ev.event_id, title=title, headline=headline,
            summary=review.summary if review else None, review=review, description=ev.description,
            topic=ev.topic, subtopic=ev.subtopic, countries=ev.countries, publishers=ev.publishers,
            independent_sources=indep, article_count=len(articles), importance=round(_clamp(importance / 100) * 100, 1),
            components=comps, penalties=penalties, flags=flags, rationale="",
            first_article_at=ev.first_article_at, latest_article_at=ev.latest_article_at,
            cluster_confidence=ev.cluster_confidence, is_continuing=ev.is_continuing,
        )

    def rank(self, events: list[EventCandidate], articles_by_id: dict[str, Article],
             reviews: dict[str, EventReview] | None = None, review_weight: float = 0.5) -> list[RankedEvent]:
        reviews = reviews or {}
        scored = [self.score(ev, [articles_by_id[a] for a in ev.article_ids if a in articles_by_id],
                             reviews.get(ev.event_id), review_weight) for ev in events]
        scored.sort(key=lambda r: (-r.importance, -r.independent_sources, r.event_id))
        selected: list[RankedEvent] = []
        per_topic: Counter = Counter()
        reasons: dict[str, str] = {}

        def try_select(r: RankedEvent, floor: float, fill: bool) -> None:
            if r.selected:
                return
            if r.importance < floor:
                reasons.setdefault(r.event_id, f"score {r.importance} below threshold {floor}")
                return
            if per_topic[r.topic] >= self.cfg.max_per_topic:
                reasons[r.event_id] = f"topic cap reached ({self.cfg.max_per_topic} {r.topic} stories already selected)"
                return
            if "soft_news" in r.flags:
                reasons[r.event_id] = "soft news is excluded from the briefing"
                return
            r.selected = True
            if fill:
                r.flags.append("quiet_day_fill")
            per_topic[r.topic] += 1
            selected.append(r)

        for r in scored:
            if len(selected) >= self.cfg.max_events:
                break
            try_select(r, self.cfg.min_score, fill=False)
        if len(selected) < self.cfg.min_events:
            for r in scored:
                if len(selected) >= self.cfg.min_events:
                    break
                try_select(r, self.cfg.quiet_day_floor, fill=True)

        selected.sort(key=lambda r: (-r.importance, -r.independent_sources, r.event_id))
        for i, r in enumerate(selected, 1):
            r.rank = i
        for r in scored:
            r.rationale = self.explain(r, reasons.get(r.event_id))
        return selected + [r for r in scored if not r.selected]

    def explain(self, r: RankedEvent, reason: str | None) -> str:
        weighted = sorted(r.components, key=lambda c: -(c.value * c.weight))
        drivers = [f"{LABELS[c.name]} {c.value:.2f} ({c.evidence[0]})" for c in weighted[:3] if c.value > 0.15]
        head = (f"Selected #{r.rank} with importance {r.importance}/100." if r.selected
                else f"Not selected (importance {r.importance}/100): {reason or 'outside the top events'}.")
        parts = [head]
        if drivers:
            parts.append("Main drivers: " + "; ".join(drivers) + ".")
        if r.penalties:
            parts.append("Penalties: " + "; ".join(r.penalties) + ".")
        if "quiet_day_fill" in r.flags:
            parts.append("Included below the normal threshold because few events qualified today.")
        if r.is_continuing:
            parts.append("Continuing story from an earlier run.")
        if r.review is not None:
            parts.append(f"Scores blend keyword analysis with a review by {r.review.model}.")
        return " ".join(parts)
