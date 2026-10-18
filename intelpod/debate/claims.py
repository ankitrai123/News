"""Claim-level evidence map (deterministic).

Claims are VERBATIM text from retrieved sources: headlines, the short extracts and the attributed
statements Phase 2 kept. Each is typed (factual / attributed / interpretation / forecast / value
judgement), grouped with matching statements from other sources, linked to contradicting ones,
and given calibrated confidence:

  confirmed   primary source, or >= 2 independent publishers, and not contradicted
  reported    a single source (copies of one report count once)
  plausible   an interpretation argued by a source
  contested   sources contradict each other
  speculative a forecast (about the future or intentions)

A value judgement is shown as the source's view ("reported"); it is never "confirmed".
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..research.evidence import split_sentences
from ..research.models import ResearchResult
from ..textutil import stable_hash, tokenize
from .models import Claim

FORECAST = re.compile(r"\b(will|would|is expected to|are expected to|expects?|expected|forecasts?|projected|projects|"
                      r"likely to|unlikely to|set to|poised to|could|may|might|outlook|next year|next month)\b", re.I)
VALUE = re.compile(r"\b(should|must|ought to|wrong|mistake|misguided|reckless|shameful|disastrous|unacceptable|"
                   r"welcomes?|welcomed|right to|deserves?|wise|unwise|foolish|betrayal|courageous)\b", re.I)
ATTRIB = re.compile(r"\b(said|says|told|according to|announced|stated|added|warned|argued|noted|wrote|"
                    r"confirmed|denied|estimated|insisted|claimed)\b", re.I)
INTERPRET = re.compile(r"\b(means|signals?|suggests?|reflects?|underscores?|because|due to|aimed at|in an effort to|"
                       r"designed to|amounts to|is seen as|a sign that|a bid to)\b", re.I)
DENIAL = re.compile(r"\b(denied|denies|rejected|rejects|dismissed|dismisses|false|untrue|refuted|no evidence)\b", re.I)
NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|per ?cent|bps|basis points|billion|million|trillion)?", re.I)
SPEAKER = [re.compile(r"^(?P<s>[A-Z][\w'’.\- ]{2,80}?)\s+(?:said|says|told|announced|warned|argued|noted|wrote|"
                      r"stated|insisted|claimed|confirmed|denied|added)\b"),
           re.compile(r"\baccording to (?:the )?(?P<s>[A-Za-z][\w'’.\- ]{2,60}?)(?:[,.;]|$)", re.I),
           re.compile(r"[,\"”]\s*(?:said|says|told|warned|added)\s+(?P<s>[A-Z][\w'’.\- ]{2,60}?)(?:[,.;]|$)")]
STAKEHOLDERS: list[tuple[str, re.Pattern]] = [
    ("international", re.compile(r"\b(imf|world bank|wto|united nations|\bun\b|nato|opec|european commission|"
                                 r"oecd|g20|g7)\b", re.I)),
    ("government", re.compile(r"\b(minister|ministry|government|president|prime minister|officials?|spokes(?:man|woman|"
                              r"person)|treasury|white house|kremlin|central bank|governor|federal reserve|fed|rbi|ecb|"
                              r"department|parliament|lawmakers?|senators?|congress|embassy|regulator)\b", re.I)),
    ("labour", re.compile(r"\b(unions?|workers|employees|labou?r leaders?|trade unionists?)\b", re.I)),
    ("consumer", re.compile(r"\b(consumers?|households|shoppers|motorists|customers|residents)\b", re.I)),
    ("investor", re.compile(r"\b(investors?|traders?|strategists?|fund managers?|bondholders|markets?)\b", re.I)),
    ("corporate", re.compile(r"\b(company|companies|ceo|chief executive|executives?|firms?|industry|chamber of commerce|"
                             r"business groups?|refiners?|manufacturers?|exporters?|lenders?)\b", re.I)),
    ("expert", re.compile(r"\b(economists?|analysts?|professor|researchers?|experts?|think tank|institute|scholars?)\b", re.I)),
    ("civil_society", re.compile(r"\b(activists?|campaigners?|ngos?|rights groups?|civil society|charity)\b", re.I)),
]
FACT_KINDS = ("factual", "attributed")


def classify_claim(text: str, content_type: str) -> tuple[str, str]:
    if content_type == "opinion" and VALUE.search(text):
        return "value_judgement", f"evaluative wording ({VALUE.search(text).group(1)})"
    if FORECAST.search(text):
        return "forecast", f"future/likelihood wording ({FORECAST.search(text).group(1)})"
    if VALUE.search(text) or (content_type == "opinion" and not ATTRIB.search(text)):
        m = VALUE.search(text)
        return "value_judgement", f"evaluative wording ({m.group(1)})" if m else "opinion piece"
    if ATTRIB.search(text):
        return "attributed", f"attributed ({ATTRIB.search(text).group(1)})"
    if INTERPRET.search(text) or content_type in ("analysis", "opinion"):
        m = INTERPRET.search(text)
        return "interpretation", f"explanatory wording ({m.group(1)})" if m else f"{content_type} source"
    return "factual", "plain statement"


def speaker_of(text: str) -> str | None:
    for rx in SPEAKER:
        m = rx.search(text)
        if m:
            s = m.group("s").strip(" ,.;")
            if 2 < len(s) <= 60 and len(s.split()) <= 8:
                return s
    return None


def stakeholder_of(text: str, source_type: str | None = None) -> str | None:
    if source_type in ("official",):
        return "government"
    if source_type in ("international_org",):
        return "international"
    if source_type in ("think_tank", "academic"):
        return "expert"
    for name, rx in STAKEHOLDERS:
        if rx.search(text):
            return name
    return None


def _units(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for num, unit in NUMBER.findall(text):
        u = (unit or "").lower().replace(" ", "").replace("percent", "%").replace("basispoints", "bps")
        out.setdefault(u, set()).add(num.replace(",", ""))
    return out


def _same_claim(a: str, b: str, ta: set[str], tb: set[str]) -> bool:
    if not ta or not tb:
        return False
    if bool(DENIAL.search(a)) != bool(DENIAL.search(b)):
        return False  # "X happened" and "officials denied X happened" are opposite claims
    ua, ub = _units(a), _units(b)
    for unit in set(ua) & set(ub):
        if unit and not ua[unit] & ub[unit]:
            return False  # same kind of figure, different value: not the same claim
    jacc = len(ta & tb) / len(ta | tb)
    if jacc >= 0.5 or fuzz.token_set_ratio(a.lower(), b.lower()) >= 85:
        return True
    # The same specific figure ("5.25%", "25 bps") about the same subject is the same fact.
    shared_figures = any(unit and ua[unit] & ub[unit] for unit in set(ua) & set(ub))
    return shared_figures and len(ta & tb) >= 2 and jacc >= 0.15


def _contradicts(a: str, b: str, ta: set[str], tb: set[str]) -> bool:
    """Conservative: same subject (>= 3 shared content words) and either different values for the
    same unit (5.25% vs 5.5%), or one side explicitly denies/rejects."""
    if len(ta & tb) < 3:
        return False
    ua, ub = _units(a), _units(b)
    if any(unit and not ua[unit] & ub[unit] for unit in set(ua) & set(ub)):
        return True
    return bool(DENIAL.search(a)) != bool(DENIAL.search(b))


NOT_A_CLAIM = re.compile(r"^(analysis|opinion|comment|commentary|explainer|explained|editorial|live|watch|video|"
                         r"podcast|q&a|factbox|timeline)\b\s*[:|\-–]|\?\s*$|^(what|why|how|who|will|can|should|is|are|"
                         r"does|do)\b", re.I)


def statements(r: ResearchResult) -> list[tuple[str, str]]:
    """(text, origin) units a source contributes: headline, extracts, attributed sentences.
    Headlines that label a piece ("Analysis: ...") or ask a question are not claims."""
    title = r.title.strip()
    out = [] if NOT_A_CLAIM.search(title) else [(title, "headline")]
    for n in r.evidence_notes:
        if n.kind == "extract":
            out.extend((s, "extract") for s in split_sentences(n.text))
    out.extend((c, "attributed") for c in r.key_claims)
    seen, uniq = set(), []
    for t, o in out:
        k = " ".join(t.lower().split())
        if len(t.split()) >= 5 and k not in seen:
            seen.add(k)
            uniq.append((t, o))
    return uniq


def build_claims(results: list[ResearchResult]) -> list[Claim]:
    """Group statements across sources into claims; link contradictions; set confidence."""
    by_id = {r.result_id: r for r in results}
    groups: list[dict] = []
    for r in results:
        for text, origin in statements(r):
            toks = set(tokenize(text))
            kind, why = classify_claim(text, r.content_type)
            if origin == "headline" and kind == "value_judgement" and r.content_type != "opinion":
                kind, why = "factual", "news headline"
            match = next((g for g in groups if g["kind"] in (kind,) + (FACT_KINDS if kind in FACT_KINDS else ())
                          and any(_same_claim(t, text, tk, toks) for t, tk in g["members"])), None)
            if match is None:
                groups.append({"text": text, "toks": toks, "kind": kind, "why": why, "ids": [r.result_id],
                               "origin": origin, "members": [(text, toks)]})
                continue
            match["members"].append((text, toks))  # later statements may match any wording seen so far
            if r.result_id not in match["ids"]:
                match["ids"].append(r.result_id)
            if origin == "extract" and match["origin"] == "headline":  # prefer a full sentence
                match.update(text=text, toks=toks, origin=origin)
    claims: list[Claim] = []
    for g in groups:
        srcs = [by_id[i] for i in g["ids"]]
        pubs = {s.publisher.lower() for s in srcs}
        primary = any(s.content_type == "primary" or s.source_type in ("official", "international_org") for s in srcs)
        cid = stable_hash(*sorted(g["ids"]), g["text"].lower())
        speaker = speaker_of(g["text"]) if g["kind"] in ("attributed", "forecast", "value_judgement") else None
        stake = stakeholder_of(speaker or g["text"], srcs[0].source_type if len(srcs) == 1 else None) \
            if (speaker or g["kind"] == "attributed" or primary) else None
        claims.append(Claim(claim_id=cid, text=g["text"], kind=g["kind"], kind_evidence=g["why"], source_ids=g["ids"],
                            independent_sources=len(pubs), primary=primary, speaker=speaker, stakeholder=stake))
    toks = {c.claim_id: set(tokenize(c.text)) for c in claims}
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if a.kind in FACT_KINDS and b.kind in FACT_KINDS and set(a.source_ids).isdisjoint(b.source_ids) \
                    and _contradicts(a.text, b.text, toks[a.claim_id], toks[b.claim_id]):
                a.contradicted_by = list(dict.fromkeys(a.contradicted_by + b.source_ids))
                b.contradicted_by = list(dict.fromkeys(b.contradicted_by + a.source_ids))
    for c in claims:
        c.confidence, c.confidence_reason = calibrate(c, by_id)
    claims.sort(key=lambda c: (-c.independent_sources, c.kind != "factual", c.text))
    return claims


def _weight(ids: list[str], by_id: dict[str, ResearchResult]) -> tuple[int, bool]:
    srcs = [by_id[i] for i in ids if i in by_id]
    primary = any(s.content_type == "primary" or s.source_type in ("official", "international_org") for s in srcs)
    return len({s.publisher.lower() for s in srcs}), primary


def calibrate(c: Claim, by_id: dict[str, ResearchResult] | None = None) -> tuple[str, str]:
    if c.contradicted_by:
        n_against, primary_against = _weight(c.contradicted_by, by_id or {})
        # Asymmetric evidence is reported as such, not as a 50/50 dispute.
        if (c.primary and not primary_against and c.independent_sources >= 2 * max(1, n_against)) or \
                (c.independent_sources >= 3 * max(1, n_against) and not primary_against):
            return "confirmed", (f"{'a primary source and ' if c.primary else ''}{c.independent_sources} publishers; "
                                 f"{n_against} source(s) report differently")
        return "contested", f"contradicted by {n_against} source(s)"
    if c.kind == "forecast":
        return "speculative", "about the future; cannot be verified yet"
    if c.kind == "interpretation":
        return "plausible", f"an interpretation argued by {c.independent_sources} source(s)"
    if c.kind == "value_judgement":
        return "reported", "a value judgement: the source's view, not a verifiable fact"
    if c.primary:
        return "confirmed", "stated by a primary (official) source"
    if c.independent_sources >= 2:
        return "confirmed", f"stated by {c.independent_sources} independent publishers"
    return "reported", "a single source; not independently confirmed"
