"""Article de-duplication.

Three layers, cheapest first:
  1. Same canonical URL                         -> same article (dropped).
  2. Same normalized title from same publisher  -> duplicate (dropped).
  3. Near-identical title from *different* publishers -> kept, but assigned the
     same `syndication_group` so wire copies are not counted as independent
     corroboration during ranking.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from ..schemas import Article

NEAR_DUP_SCORE = 92  # token_set_ratio threshold for "same headline"
MIN_WORDS_FOR_FUZZY = 6


@dataclass
class DedupStats:
    input: int = 0
    duplicate_url: int = 0
    duplicate_title_same_publisher: int = 0
    syndicated: int = 0
    kept: int = 0
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "examples"} | {"examples": self.examples[:10]}


@dataclass
class _Known:
    canonical_url: str
    normalized_title: str
    source_name: str
    syndication_group: str


def deduplicate(new: list[Article], existing: list[_Known] | list[Article] | None = None) -> tuple[list[Article], DedupStats]:
    """Return articles not already known, with syndication groups resolved.

    `existing` are recent articles already in storage (Article or anything with the
    same four attributes)."""
    stats = DedupStats(input=len(new))
    known_urls = {e.canonical_url for e in existing or []}
    by_title_pub = {(e.normalized_title, e.source_name.lower()) for e in existing or []}
    groups: list[tuple[str, str]] = [(e.normalized_title, e.syndication_group) for e in existing or []]
    exact_group = {t: g for t, g in groups}
    titles = [t for t, _ in groups]
    kept: list[Article] = []

    for art in new:
        if art.canonical_url in known_urls:
            stats.duplicate_url += 1
            continue
        key = (art.normalized_title, art.source_name.lower())
        if key in by_title_pub:
            stats.duplicate_title_same_publisher += 1
            stats.examples.append(f"same-publisher duplicate: {art.title}")
            continue

        group = exact_group.get(art.normalized_title)
        if group is None and len(art.normalized_title.split()) >= MIN_WORDS_FOR_FUZZY and titles:
            # C-speed scan; then require overall similarity too so a short headline that is a
            # subset of a long one is not mistaken for a copy.
            for title, _score, idx in process.extract(art.normalized_title, titles, scorer=fuzz.token_set_ratio,
                                                      score_cutoff=NEAR_DUP_SCORE, limit=5):
                if fuzz.ratio(title, art.normalized_title) >= NEAR_DUP_SCORE - 10:
                    group = groups[idx][1]
                    break
        if group is not None and group != art.syndication_group:
            art = art.model_copy(update={"syndication_group": group})
        if group is not None:
            stats.syndicated += 1
            stats.examples.append(f"syndicated copy: {art.source_name}: {art.title}")

        known_urls.add(art.canonical_url)
        by_title_pub.add(key)
        groups.append((art.normalized_title, art.syndication_group))
        titles.append(art.normalized_title)
        exact_group.setdefault(art.normalized_title, art.syndication_group)
        kept.append(art)

    stats.kept = len(kept)
    return kept, stats
