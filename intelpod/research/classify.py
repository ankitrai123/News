"""What kind of source a result is, and which research questions it serves (rules, no model)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..textutil import find_countries

_PATH_RULES: list[tuple[str, re.Pattern]] = [
    ("opinion", re.compile(r"/(opinions?|commentisfree|comment|editorials?|op-?eds?|columns?|columnists?|blogs?|"
                           r"letters|views?|perspectives?)(/|$)")),
    ("analysis", re.compile(r"/(analysis|explainers?|explained|insights?|commentary|in-depth|deep-dive|"
                            r"long-?reads?|the-big-story)(/|$)")),
    ("research", re.compile(r"/(research|publications?|working-?papers?|reports?|papers|studies|policy-briefs?)(/|$)")),
    ("primary", re.compile(r"/(press-?releases?|pressreleases?|newsroom|press|speeches|statements?|"
                           r"press-?room|media-?centre|media-?center)(/|$)")),
]
_TITLE_RULES: list[tuple[str, re.Pattern]] = [
    ("opinion", re.compile(r"^(opinion|op-?ed|editorial|comment|column|view|letter)\s*[:|—-]", re.I)),
    ("analysis", re.compile(r"^(analysis|explained|explainer|fact ?check|q&a|insight)\s*[:|—-]|"
                            r"^(why|how|what)\b.*\b(means?|matters?|next|happened)\b|\bexplained\b", re.I)),
]
_SCHEMA = {
    "opinionnewsarticle": "opinion", "analysisnewsarticle": "analysis", "reportagenewsarticle": "reporting",
    "backgroundnewsarticle": "analysis", "reviewnewsarticle": "opinion", "scholarlyarticle": "research",
    "report": "research", "pressrelease": "primary", "newsarticle": "reporting", "blogposting": "opinion",
}
_SECTION = {"commentisfree": "opinion", "opinion": "opinion", "comment": "opinion", "analysis": "analysis"}

CUES: dict[str, re.Pattern] = {
    "criticism": re.compile(r"\b(critic\w*|backlash|oppos\w+|condemn\w*|slam\w*|concerns?|risks?|warn\w*|"
                            r"flaw\w*|controvers\w*|object\w*)\b", re.I),
    "expert_analysis": re.compile(r"\b(analysts?|experts?|economists?|strategists?|professor|researchers?|"
                                  r"analysis|think[- ]tank|commentary)\b", re.I),
    "business_impact": re.compile(r"\b(compan\w+|industr\w+|firms?|shares|stocks?|investors?|exporters?|importers?|"
                                  r"supply chains?|earnings|profits?|revenue|refiners?|manufacturers?|banks?)\b", re.I),
    "economic_views": re.compile(r"\b(econom\w+|inflation|growth|gdp|markets?|prices|rates?|recession|"
                                 r"fiscal|monetary|trade deficit)\b", re.I),
    "political_views": re.compile(r"\b(opposition|lawmakers?|parliament|congress|senators?|ministers?|party|"
                                  r"government|election|politic\w+|diplomat\w*)\b", re.I),
    "open_questions": re.compile(r"\b(uncertain\w*|unclear|unknown|outlook|what next|what comes next|"
                                 r"remains to be seen|open questions?|questions remain|unanswered)\b", re.I),
}


def classify_content(url: str, title: str, section: str | None, source_type: str | None,
                     schema_type: str | None) -> tuple[str, str]:
    """(content type, why). First matching rule wins; the reason is shown to the user."""
    st = (source_type or "").lower()
    path = urlsplit(url or "").path.lower()
    if st in ("official", "international_org"):
        return "primary", f"published by an {'official body' if st == 'official' else 'international organisation'}"
    if schema_type:
        mapped = _SCHEMA.get(schema_type.replace(" ", "").lower())
        if mapped and mapped != "reporting":
            return mapped, f"page declares itself {schema_type}"
    for kind, rx in _PATH_RULES:
        if rx.search(path):
            return kind, f"URL section ({rx.search(path).group(1)})"
    if section and (sec := _SECTION.get(section.lower())):
        return sec, f"publisher section '{section}'"
    for kind, rx in _TITLE_RULES:
        if rx.search(title or ""):
            return kind, "headline wording"
    if st in ("think_tank", "academic"):
        return "research", f"published by a {st.replace('_', ' ')}"
    if schema_type and _SCHEMA.get(schema_type.replace(" ", "").lower()) == "reporting":
        return "reporting", f"page declares itself {schema_type}"
    if st in ("news", ""):
        return "reporting", "news outlet, no opinion or analysis markers"
    return "unknown", "no markers"


def label_query_types(title: str, snippet: str | None, notes_text: str, content_type: str, source_type: str | None,
                      countries: list[str], found_by: list[str]) -> list[str]:
    """Query types a result actually serves: the queries that found it, plus what its own text shows."""
    text = f"{title} {snippet or ''} {notes_text}"
    out = list(dict.fromkeys(found_by))
    # A broad local search "covers" every type; keep only types the text supports, plus history/facts.
    supported = {"latest_facts", "recent_history"}
    for qt, rx in CUES.items():
        if rx.search(text):
            supported.add(qt)
    if content_type in ("analysis", "research"):
        supported.add("expert_analysis")
    if "India" in find_countries(text):
        supported.add("india_impact")
    if content_type == "primary" or (source_type or "") in ("official", "international_org"):
        supported.add("primary_sources")
    labelled = [qt for qt in out if qt in supported]
    for qt in ("latest_facts", "recent_history", "primary_sources", "expert_analysis", "political_views",
               "economic_views", "business_impact", "india_impact", "criticism", "open_questions"):
        if qt in supported and qt not in labelled and qt not in ("latest_facts", "recent_history"):
            labelled.append(qt)
    return labelled or ["recent_history"]
