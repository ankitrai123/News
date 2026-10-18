"""Merge search hits into candidates, select a diverse research package, state its limits."""
from __future__ import annotations

from rapidfuzz import fuzz

from ..textutil import canonicalize_url, domain_of, normalize_title
from .models import QUERY_TYPE_LABELS, QUERY_TYPES, Candidate, DateCandidate, QueryStatus, ResearchResult, SearchHit

NEAR_DUP = 92  # RapidFuzz token_sort_ratio for "the same headline, lightly edited"


def _canon(url: str) -> str | None:
    try:
        return canonicalize_url(url)
    except ValueError:
        return None


def dedupe_hits(hits: list[SearchHit], event_urls: set[str]) -> list[Candidate]:
    """One candidate per page (canonical URL); identical headlines from several publishers are
    one story carried by many (syndication): the earliest stays, the others are listed."""
    by_url: dict[str, Candidate] = {}
    for h in hits:
        canon = _canon(h.url)
        if not canon or not h.url.startswith(("http://", "https://")):
            continue
        c = by_url.get(canon)
        if c is None:
            c = by_url[canon] = Candidate(canonical_url=canon, url=h.url, title=h.title.strip(),
                                          publisher=h.publisher or h.domain or domain_of(h.url) or "unknown",
                                          domain=h.domain or domain_of(h.url), snippet=h.snippet, language=h.language,
                                          source_country=h.source_country, source_type=h.source_type,
                                          section=h.section)
        c.providers = list(dict.fromkeys(c.providers + [h.provider]))
        c.query_ids = list(dict.fromkeys(c.query_ids + [h.query_id]))
        c.query_types = list(dict.fromkeys(c.query_types + [h.query_type]))
        c.dates.append(DateCandidate(value=h.published_at, source=h.date_source))
        c.snippet = c.snippet or h.snippet
        c.source_type = c.source_type or h.source_type
        c.section = c.section or h.section
        c.from_event = c.from_event or h.from_event or canon in event_urls
    cands = list(by_url.values())

    def first_date(c: Candidate):
        ds = [d.value for d in c.dates if d.value is not None]
        return (min(ds).timestamp() if ds else float("inf"), c.canonical_url)

    groups: dict[str, list[Candidate]] = {}
    for c in cands:
        groups.setdefault(normalize_title(c.title), []).append(c)
    keep: list[Candidate] = []
    for members in groups.values():
        members.sort(key=first_date)
        rep, rest = members[0], members[1:]
        pubs = [m.publisher for m in rest if m.publisher != rep.publisher]
        if pubs:
            rep.syndicated_with = list(dict.fromkeys(rep.syndicated_with + pubs))
        for m in rest:
            rep.providers = list(dict.fromkeys(rep.providers + m.providers))
            rep.query_types = list(dict.fromkeys(rep.query_types + m.query_types))
            rep.from_event = rep.from_event or m.from_event
        keep.append(rep)
    # The same publisher running a lightly edited headline twice: keep one.
    keep.sort(key=first_date)
    out: list[Candidate] = []
    for c in keep:
        if any(o.publisher == c.publisher and fuzz.token_sort_ratio(normalize_title(o.title), normalize_title(c.title)) >= NEAR_DUP
               for o in out):
            continue
        out.append(c)
    return out


def select_results(results: list[ResearchResult], cfg) -> list[ResearchResult]:
    """Greedy, deterministic choice of up to `max_results` sources: relevant, credible, and
    covering as many research questions, kinds of source and publishers as possible."""
    def value(r: ResearchResult) -> float:
        cred = r.credibility_assessment.score
        return r.relevance_score * (0.5 + 0.5 * (cred if cred is not None else 0.5))

    pool = []
    for r in results:
        r.selected = False
        if r.relevance_score < cfg.min_relevance:
            r.selection_reason = f"not used: relevance {r.relevance_score:.2f} below {cfg.min_relevance:.2f}"
        else:
            pool.append(r)
    pool.sort(key=lambda r: (-value(r), r.result_id))
    chosen: list[ResearchResult] = []
    covered_q: set[str] = set()
    covered_c: set[str] = set()
    per_pub: dict[str, int] = {}
    from_event = 0
    while pool and len(chosen) < cfg.max_results:
        best, best_score, why = None, -1.0, ""
        for r in pool:
            new_q = [q for q in r.query_types if q not in covered_q]
            gain = value(r) + 0.15 * bool(new_q) + 0.08 * (r.content_type not in covered_c) + \
                0.05 * (r.publisher not in per_pub)
            if gain > best_score:
                best, best_score = r, gain
                why = ("adds " + ", ".join(QUERY_TYPE_LABELS[q].lower() for q in new_q[:2])) if new_q else \
                    "highly relevant"
        pool.remove(best)
        if per_pub.get(best.publisher, 0) >= cfg.max_per_publisher:
            best.selection_reason = f"not used: already {cfg.max_per_publisher} sources from {best.publisher}"
            continue
        if best.from_event and from_event >= cfg.max_from_event:
            best.selection_reason = f"not used: already {cfg.max_from_event} of the event's own reports"
            continue
        dup = next((c for c in chosen if fuzz.token_sort_ratio(normalize_title(c.title), normalize_title(best.title)) >= 85), None)
        if dup is not None:
            best.selection_reason = f"not used: near-duplicate of \"{dup.title[:80]}\""
            continue
        best.selected = True
        best.selection_reason = f"selected: {why} (relevance {best.relevance_score:.2f})"
        chosen.append(best)
        covered_q.update(best.query_types)
        covered_c.add(best.content_type)
        per_pub[best.publisher] = per_pub.get(best.publisher, 0) + 1
        from_event += best.from_event
    for r in pool:
        if not r.selection_reason:
            r.selection_reason = f"not used: {cfg.max_results} stronger sources already selected"
    return chosen


def package_limitations(selected: list[ResearchResult], results: list[ResearchResult], queries: list[QueryStatus],
                        cfg, notes: list[str]) -> list[str]:
    """Plain statements of what this research could not establish."""
    out: list[str] = []
    n = len(selected)
    if n == 0:
        out.append("No relevant sources were found; nothing in this package can be cited.")
    elif n < cfg.min_results:
        out.append(f"Only {n} relevant source{'s' if n != 1 else ''} found (target {cfg.min_results}-{cfg.max_results}).")
    covered = {q for r in selected for q in r.query_types}
    missing = [QUERY_TYPE_LABELS[q] for q in QUERY_TYPES if q not in covered and q not in ("latest_facts", "recent_history")]
    if selected and missing:
        out.append("Nothing found for: " + ", ".join(m.lower() for m in missing) + ".")
    if selected and not any(r.content_type == "primary" for r in selected):
        out.append("No primary source (official statement or document) among the selected sources.")
    kinds = {r.content_type for r in selected}
    if len(selected) >= 3 and len(kinds) == 1:
        out.append(f"All selected sources are {next(iter(kinds))}; no other kind of source was found.")
    pubs = {r.publisher for r in selected}
    if len(selected) >= 3 and len(pubs) <= 2:
        out.append(f"Selected sources come from only {len(pubs)} publisher(s).")
    walled = sum(1 for r in results if r.page_status == "paywalled")
    robots = sum(1 for r in results if r.page_status == "robots_disallowed")
    optout = sum(1 for r in results if r.page_status == "metadata_only")
    if walled:
        out.append(f"{walled} page(s) are paywalled or behind a login: only their headline and date were used.")
    if robots:
        out.append(f"{robots} page(s) disallow automated access (robots.txt) and were not read.")
    if optout:
        out.append(f"{optout} page(s) were used for metadata only (non-HTML, or the publisher opts out of text use).")
    approx = sum(1 for r in selected if r.date_source in ("gdelt_seendate", "missing"))
    if selected and approx * 2 >= len(selected):
        out.append(f"{approx} of {n} selected sources have no publisher date (approximate or missing).")
    failed = sorted({q.provider for q in queries if q.status in ("error", "rate_limited")})
    if failed:
        out.append("Search problems with: " + ", ".join(failed) + " (see the queries list).")
    out.extend(n_ for n_ in notes if n_ not in out)
    return out
