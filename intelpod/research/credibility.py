"""Transparent source-quality assessment for one research result.

Each component is a value in [0, 1] with the evidence behind it, or None ("unknown") when
IntelPod cannot judge it; unknown components are left out of the score and listed. There is no
"famous publisher" component: reputation is not evidence that a claim is true, and repetition is
not corroboration. Track record and corrections policy stay unknown until Phase 3's source
registry supplies documented, human-reviewable evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import CredibilityAssessment, CredibilityComponent, EventBrief, PageInfo, ResearchResult

WEIGHTS = {"proximity": 1.0, "transparency": 0.8, "evidence": 0.8, "independence": 0.8, "recency": 0.5,
           "relevance": 1.0, "expertise": 0.6, "track_record": 0.8, "corrections_policy": 0.4}
EXACT_DATE = ("page_metadata", "feed", "api")
TOPIC_EXPERTS = {
    "markets_monetary": ("central bank", "reserve bank", "federal reserve", "ecb", "rbi", "imf", "bis", "treasury",
                         "finance ministry", "bank of"),
    "trade_policy": ("wto", "trade", "commerce", "ustr", "customs"),
    "energy_commodities": ("opec", "iea", "energy", "eia"),
    "macroeconomy": ("imf", "world bank", "oecd", "statistics", "finance ministry", "central bank"),
}


def assess(result: ResearchResult, page: PageInfo | None, brief: EventBrief,
           peers: list[ResearchResult]) -> CredibilityAssessment:
    comps: list[CredibilityComponent] = []
    flags: list[str] = []

    def add(name: str, value: float | None, evidence: str) -> None:
        comps.append(CredibilityComponent(name=name, value=None if value is None else round(max(0, min(1, value)), 2),
                                          weight=WEIGHTS[name], evidence=evidence))

    # proximity to the evidence
    ct = result.content_type
    # dedupe keeps the EARLIEST outlet of a widely carried headline (the likely originator) and
    # lists the others in `syndicated_with`; copies themselves are never kept.
    widely_carried = bool(result.syndicated_with)
    if ct == "primary":
        add("proximity", 1.0, "primary source (official statement or document)")
    elif ct == "research":
        add("proximity", 0.8, "research publication")
    elif ct == "reporting":
        add("proximity", 0.7, "news reporting")
    elif ct == "analysis":
        add("proximity", 0.6, "analysis of reported facts")
    elif ct == "opinion":
        add("proximity", 0.5, "opinion: argues a view")
        flags.append("opinion")
    else:
        add("proximity", None, "content type unknown")

    # transparency (observable signals only)
    fetched = page is not None and page.status in ("fetched", "metadata_only", "paywalled")
    if fetched or result.date_source in EXACT_DATE:
        signals = {"named author": bool(page and page.author),
                   "exact publication date": result.date_source in EXACT_DATE,
                   "identified publisher": bool(result.publisher and result.publisher != result.domain),
                   "corrections/standards link": bool(page and page.has_corrections_link)}
        present = [k for k, v in signals.items() if v]
        add("transparency", len(present) / len(signals), ("has: " + ", ".join(present)) if present else "no signals found")
    else:
        add("transparency", None, "page not fetched")

    # evidence and citations
    if page is not None and page.status == "fetched":
        links = page.outbound_primary_links
        notes = " ".join(n.text for n in result.evidence_notes)
        has_numbers = any(ch.isdigit() for ch in notes)
        has_quotes = bool(result.key_claims)
        v = min(1.0, 0.25 * min(links, 2) + 0.25 * has_numbers + 0.25 * has_quotes + (0.25 if links >= 3 else 0))
        add("evidence", v, f"{links} link(s) to primary documents; "
                           f"{'specific figures' if has_numbers else 'no figures'}; "
                           f"{'attributed statements' if has_quotes else 'no attributed statements'} in the extracts")
    else:
        add("evidence", None, "page text not available")

    # independence
    same_pub = sum(1 for p in peers if p.publisher == result.publisher and p.result_id != result.result_id)
    v = 1.0 - min(0.4, 0.1 * same_pub)
    if widely_carried:
        flags.append("widely_carried")
    add("independence", v, (f"also carried by {len(result.syndicated_with)} other outlet(s), counted once; "
                            if widely_carried else "")
        + (f"{same_pub} other result(s) from {result.publisher}" if same_pub else "no other results from this publisher"))

    # recency
    now = datetime.now(timezone.utc)
    if result.published_at is None:
        add("recency", None, "undated")
    elif not result.in_window:
        add("recency", 0.3, "background: older than the research window")
        flags.append("background")
    else:
        age = now - result.published_at
        v = 1.0 if age <= timedelta(days=3) else max(0.5, 1.0 - (age.days - 3) * 0.03)
        add("recency", v, f"published {result.published_at:%Y-%m-%d}")
    if result.date_source == "gdelt_seendate":
        flags.append("date_approximate")

    add("relevance", result.relevance_score, result.relevance_evidence or "")

    # subject expertise (only where the source type makes it evident)
    pub = (result.publisher or "").lower()
    experts = TOPIC_EXPERTS.get(brief.topic, ())
    if ct == "primary" and any(k in pub for k in experts):
        add("expertise", 1.0, f"{result.publisher} is a responsible authority for {brief.topic.replace('_', ' ')}")
    elif result.source_type in ("think_tank", "academic"):
        add("expertise", 0.8, f"{result.source_type.replace('_', ' ')} publication")
    else:
        add("expertise", None, "subject expertise not established")

    add("track_record", None, "needs the Phase 3 source registry (documented evidence), not reputation")
    add("corrections_policy", None, "not recorded yet")

    # interested parties: an official voice of a country or body that is a party to the event
    if ct == "primary" and any(c.lower() in pub or (result.domain or "").endswith(_cc_tld(c)) for c in brief.countries):
        flags.append("interested_party")
    if result.page_status in ("metadata_only", "paywalled"):
        flags.append("metadata_only" if result.page_status == "metadata_only" else "paywalled")

    known = [c for c in comps if c.value is not None]
    score = round(sum(c.value * c.weight for c in known) / sum(c.weight for c in known), 3) if known else None
    unknown = [c.name for c in comps if c.value is None]
    parts = []
    if score is not None:
        parts.append(f"quality {score:.2f} from {len(known)} of {len(comps)} criteria")
    if "interested_party" in flags:
        parts.append("a party to the event: evidence of its own position, not neutral verification")
    if "opinion" in flags:
        parts.append("opinion")
    if "widely_carried" in flags:
        parts.append("widely carried report (earliest outlet shown; copies counted once)")
    return CredibilityAssessment(score=score, components=comps, unknown=unknown, flags=list(dict.fromkeys(flags)),
                                 summary="; ".join(parts))


def _cc_tld(country: str) -> str:
    return {"India": ".gov.in", "United States": ".gov", "United Kingdom": ".gov.uk", "China": ".gov.cn",
            "Russia": ".gov.ru", "Japan": ".go.jp"}.get(country, "\u0000")
