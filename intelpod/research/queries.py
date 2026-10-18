"""Research queries for one event: ten query types, rendered per provider.

Query types (product spec): latest facts, recent history, political and economic
interpretations, business/industry consequences, India implications, expert analysis, primary
sources, criticism/counterarguments, open questions.

Providers differ in cost, so queries are rendered differently:
  * local providers ("archive", "feeds") are free: one broad query over the window; every hit is
    later labelled with the query types it serves (classify.label_query_types);
  * GDELT is slow and rate-limited: a few COMBINED queries (`covers` lists the types each serves),
    always valid GDELT syntax (checked again by providers.gdelt_query_problems before sending);
  * keyword APIs (Guardian): two queries, facts and analysis.

Keywords come from the event itself (names, distinctive phrases, top terms); optional
model-proposed keywords are sanitised and only ever used as search terms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import ResearchConfig
from ..textutil import STOPWORDS, Taxonomy, is_country_alias, stable_hash, stem, tokenize
from .models import EventBrief, ResearchQuery

CUE_WORDS: dict[str, list[str]] = {
    "political_views": ["opposition", "lawmakers", "critics", "government", "parliament", "congress"],
    "economic_views": ["economists", "economy", "inflation", "growth", "markets", "investors"],
    "business_impact": ["companies", "industry", "shares", "exporters", "supply chain", "earnings"],
    "expert_analysis": ["analysts", "experts", "analysis", "commentary", "economists", "think tank"],
    "criticism": ["criticism", "critics", "backlash", "opposed", "concerns", "risks"],
    "open_questions": ["uncertain", "unclear", "outlook", "what next", "questions", "could"],
}
ANALYSIS_CUES = ["analysis", "opinion", "editorial", "commentary", "analysts", "experts", "critics"]
INDIA_TERMS = ["India", "Indian", "New Delhi"]

# Official domains per country/organisation for primary-source searches (no dashes: GDELT bug).
OFFICIAL_DOMAINS: dict[str, list[str]] = {
    "United States": ["treasury.gov", "whitehouse.gov", "state.gov", "commerce.gov", "federalreserve.gov", "ustr.gov"],
    "India": ["pib.gov.in", "mea.gov.in", "rbi.org.in", "commerce.gov.in"],
    "United Kingdom": ["gov.uk", "bankofengland.co.uk"],
    "European Union": ["ec.europa.eu", "ecb.europa.eu", "consilium.europa.eu"],
    "China": ["mofcom.gov.cn", "fmprc.gov.cn"],
    "Japan": ["mofa.go.jp", "boj.or.jp"],
}
GLOBAL_OFFICIAL = ["imf.org", "worldbank.org", "wto.org", "un.org", "oecd.org"]

# GDELT rejects words of 2 characters or fewer: spell common abbreviations out.
EXPANSIONS = {"us": "united states", "u.s.": "united states", "uk": "britain", "eu": "european union",
              "ai": "artificial intelligence", "un": "united nations"}
_SAFE = re.compile(r"[^A-Za-z0-9 '\-]")


@dataclass
class Keywords:
    phrases: list[str] = field(default_factory=list)   # multi-word distinctive phrases
    entities: list[str] = field(default_factory=list)  # named entities (not countries)
    countries: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)     # readable single words, most distinctive first
    topic: str = ""

    def anchors(self, limit: int = 3) -> list[str]:
        out = list(dict.fromkeys(self.entities[:2] + self.phrases[:2]))
        return out[:limit]


def clean_keyword(k: str) -> str | None:
    k = " ".join(_SAFE.sub(" ", k or "").split()).strip(" -'")
    if k.lower() in EXPANSIONS:
        k = EXPANSIONS[k.lower()]
    if not (3 <= len(k) <= 40) or k.lower() in STOPWORDS or k.upper() in ("OR", "AND", "NOT"):
        return None
    return k


def event_keywords(brief: EventBrief, taxonomy: Taxonomy) -> Keywords:
    title = brief.headline or brief.title
    text = f"{title}. {brief.summary or brief.description or ''}"
    entities = [e for e in brief.entities if not is_country_alias(e)]
    entities = [c for e in entities if (c := clean_keyword(e))][:4]
    phrases: list[str] = []
    for hits in taxonomy.topic_hits(title).values():
        phrases.extend(h for h in hits if " " in h)
    phrases = list(dict.fromkeys(c for p in phrases if (c := clean_keyword(p))))[:3]
    weights = {k: v for k, v in brief.centroid.items() if not k.startswith(("ctry", "pair:"))}
    words: dict[str, float] = {}
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", text):
        lw = w.lower().strip("'-")
        if lw in STOPWORDS or is_country_alias(w) or len(lw) < 3:
            continue
        toks = tokenize(lw, taxonomy.synonyms)
        score = max((weights.get(t, 0.0) for t in toks), default=0.0) or (0.01 if w in title else 0.0)
        if score and lw not in words:
            words[lw] = score
    terms = [w for w, _ in sorted(words.items(), key=lambda kv: -kv[1])]
    names = {e.lower() for e in entities}
    terms = [t for t in terms if t not in names and not any(t in p.lower().split() for p in phrases)][:8]
    return Keywords(phrases=phrases, entities=entities, countries=list(brief.countries), terms=terms, topic=brief.topic)


def _q(term: str) -> str:
    """Quote multi-word phrases; leave single words bare."""
    return f'"{term}"' if " " in term else term


def _or(items: list[str]) -> str:
    items = [i for i in dict.fromkeys(items) if i]
    if not items:
        return ""
    if len(items) == 1:
        return _q(items[0])
    return "(" + " OR ".join(_q(i) for i in items) + ")"


def _country_word(c: str) -> str:
    return {"United States": "united states", "United Kingdom": "britain", "European Union": "european union"}.get(c, c)


def gdelt_core(kw: Keywords) -> str:
    """Event core: an OR group of its names/phrases (or top terms) AND one more distinctive word."""
    anchors = kw.anchors()
    rest = [t for t in kw.terms if len(t) >= 3]
    if anchors:
        core = _or(anchors)
        extra = rest[:1] or [_country_word(c) for c in kw.countries[:1]]
    else:
        core = _or(rest[:3])
        extra = [_country_word(c) for c in kw.countries[:1]] or rest[3:4]
    return " ".join(x for x in [core, _q(extra[0]) if extra else ""] if x)


def _fit(parts: list[str], lang: str, limit: int = 200) -> str:
    """Join query parts; drop the language filter, then trailing parts, until under the limit."""
    q = " ".join(p for p in parts + [lang] if p)
    if len(q) <= limit:
        return q
    q = " ".join(p for p in parts if p)
    while len(q) > limit and len(parts) > 1:
        parts = parts[:-1]
        q = " ".join(p for p in parts if p)
    return q[:limit]


def build_queries(brief: EventBrief, taxonomy: Taxonomy, cfg: ResearchConfig, now: datetime,
                  providers: list[str], extra_terms: dict[str, list[str]] | None = None) -> list[ResearchQuery]:
    kw = event_keywords(brief, taxonomy)
    extra = {k: [c for v in vals[:4] if (c := clean_keyword(v))] for k, vals in (extra_terms or {}).items()}
    if extra.get("latest_facts"):  # model "core" keywords sharpen the event's own anchors
        kw.phrases = list(dict.fromkeys(kw.phrases + [e for e in extra["latest_facts"] if " " in e]))[:3]
    start = now - timedelta(days=cfg.window_days)
    out: list[ResearchQuery] = []

    def add(qtype: str, provider: str, text: str, terms: list[str], covers: list[str], q_start: datetime,
            origin: str = "template") -> None:
        if not text.strip():
            return
        qid = stable_hash(brief.event_id, qtype, provider, text, q_start.isoformat(), now.isoformat())
        out.append(ResearchQuery(query_id=qid, event_id=brief.event_id, query_type=qtype, covers=covers,
                                 provider=provider, text=text, terms=terms, start=q_start, end=now, origin=origin))

    base_terms = list(dict.fromkeys(kw.anchors() + kw.terms[:5] + kw.countries[:2]))
    for p in providers:
        if p in ("archive", "feeds"):
            covers = ["latest_facts", "recent_history", "primary_sources", "expert_analysis", "political_views",
                      "economic_views", "business_impact", "india_impact", "criticism", "open_questions"]
            add("recent_history", p, ", ".join(base_terms), base_terms, covers, start)
            extra_kw = [k for vals in extra.values() for k in vals]
            if extra_kw:
                add("expert_analysis", p, ", ".join(extra_kw[:8]), extra_kw[:8], covers, start, "model")
        elif p == "gdelt":
            _gdelt_queries(kw, cfg, start, extra, add)
        elif p == "guardian":
            anchors = kw.anchors() or kw.terms[:3]
            core = _or(anchors)
            if kw.terms and kw.anchors():
                core += f" AND {kw.terms[0]}"
            add("latest_facts", p, core, base_terms, ["latest_facts", "recent_history"], start)
            add("expert_analysis", p, f"{core} AND (analysis OR opinion OR comment OR editorial)", base_terms,
                ["expert_analysis", "political_views", "economic_views", "criticism", "open_questions"], start)
    return out


def _gdelt_queries(kw: Keywords, cfg: ResearchConfig, start: datetime, extra: dict[str, list[str]], add_fn) -> None:
    """At most `max_queries_per_event` combined queries, most valuable first."""
    budget = cfg.gdelt.max_queries_per_event
    lang = cfg.gdelt.language_filter
    core = gdelt_core(kw)
    if budget <= 0 or not core:
        return
    terms = kw.anchors() + kw.terms[:4]
    plans = [
        ("latest_facts", [core], ["latest_facts", "recent_history"], start),
        ("expert_analysis", [core, _or(ANALYSIS_CUES[:5] + extra.get("expert_analysis", [])[:1])],
         ["expert_analysis", "criticism", "political_views", "economic_views", "open_questions"], start),
        ("india_impact", [core, "sourcecountry:IN"], ["india_impact"], start),
        ("business_impact", [core, _or(CUE_WORDS["business_impact"][:4] + extra.get("business_impact", [])[:1])],
         ["business_impact", "economic_views"], start),
    ]
    domains = [d for c in kw.countries for d in OFFICIAL_DOMAINS.get(c, [])][:4] or GLOBAL_OFFICIAL[:4]
    plans.append(("primary_sources", [core, _or([f"domainis:{d}" for d in domains])],
                  ["primary_sources"], start))
    if "India" in kw.countries:  # the core query already finds Indian coverage; spend the budget elsewhere
        plans = [p for p in plans if p[0] != "india_impact"] + [p for p in plans if p[0] == "india_impact"]
    for qtype, parts, covers, q_start in plans[:budget]:
        lang_part = "" if any(p.startswith("sourcecountry:") for p in parts) else lang
        add_fn(qtype, "gdelt", _fit(parts, lang_part), terms, covers, q_start)


def stem_terms(words: list[str]) -> set[str]:
    return {stem(w.lower()) for w in words}
