"""Short, verbatim evidence from page text (deterministic; no model).

Notes are whole sentences copied from the page (trimmed at a word boundary when long), chosen
for their overlap with the event's terms and names. Claims are sentences that say who said
something ("X said", "according to X"). Nothing is paraphrased here, so nothing can be invented.
"""
from __future__ import annotations

import re

from ..textutil import tokenize
from .models import EvidenceNote

_ABBREV = ("mr", "mrs", "ms", "dr", "prof", "sen", "rep", "gov", "gen", "col", "lt", "st", "jr", "sr", "inc", "ltd",
           "corp", "co", "vs", "no", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
           "dec", "u.s", "u.k", "u.n", "e.u", "a.m", "p.m", "etc", "approx", "est", "fig")
_SENT_END = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[\"'“‘(]?[A-Z0-9])")
_BOILER = re.compile(r"\b(cookie|subscribe|subscription|newsletter|sign up|sign in|log in|read more|click here|"
                     r"all rights reserved|follow us|advertisement|share this|related articles?|"
                     r"download the app|terms of use|privacy policy)\b|"
                     r"^\s*(also read|read also|must read|don'?t miss|see also|recommended|related|watch|listen|"
                     r"photo|image|file photo|trending|top stories|latest news)\b\s*[:|\-–]", re.I)
_ATTRIB = re.compile(r"\b(said|says|told|according to|announced|stated|added|warned|argued|noted|wrote|"
                     r"confirmed|denied|estimated|forecast|predicted)\b", re.I)
_NUMBER = re.compile(r"\d")


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences without breaking on "U.S.", "Mr.", "3.25%" or initials."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n|\n", text or ""):
        para = " ".join(para.split())
        if not para:
            continue
        start = 0
        for m in _SENT_END.finditer(para):
            before = para[start:m.start()].rstrip("\"'”’)")
            last = before.rsplit(" ", 1)[-1].lower().rstrip(".")
            if last in _ABBREV or re.fullmatch(r"[a-z]", last):  # "Mr.", "J. Smith"
                continue
            out.append(para[start:m.start()].strip())
            start = m.end()
        tail = para[start:].strip()
        if tail:
            out.append(tail)
    return [s for s in out if s]


def trim(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


def _usable(s: str) -> bool:
    words = len(s.split())
    return 6 <= words <= 80 and not _BOILER.search(s) and not s.endswith("?")


def _score(sentence: str, terms: set[str], entities: list[str], position: int) -> float:
    toks = set(tokenize(sentence))
    overlap = len(toks & terms)
    if not overlap:
        return 0.0
    low = sentence.lower()
    ent = sum(1 for e in entities if e.lower() in low)
    score = overlap + 1.5 * ent
    score += 0.5 if _NUMBER.search(sentence) else 0.0
    score += 0.5 if _ATTRIB.search(sentence) else 0.0
    return score * (1.0 - min(position, 40) * 0.01)  # earlier sentences slightly preferred


def _term_tokens(terms: list[str]) -> set[str]:
    return {t for term in terms for t in tokenize(term)}


def _pick(text: str, terms: list[str], entities: list[str], limit: int, need) -> list[str]:
    tt = _term_tokens(terms + entities)
    scored = []
    for i, s in enumerate(split_sentences(text)):
        if not _usable(s) or not need(s):
            continue
        sc = _score(s, tt, entities, i)
        if sc >= 2.0:  # at least two event terms (or a name), never a lone generic word
            scored.append((sc, i, s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen: list[str] = []
    for _, _, s in scored:
        key = set(tokenize(s))
        if any(len(key & set(tokenize(c))) >= 0.8 * max(1, len(key)) for c in chosen):
            continue  # near-duplicate of a sentence already chosen
        chosen.append(s)
        if len(chosen) >= limit:
            break
    return chosen


def extract_evidence(text: str | None, terms: list[str], entities: list[str], max_notes: int = 3,
                     max_chars: int = 300) -> list[EvidenceNote]:
    if not text or max_notes <= 0:
        return []
    return [EvidenceNote(text=trim(s, max_chars), kind="extract", source_field="page")
            for s in _pick(text, terms, entities, max_notes, lambda s: True)]


def extract_claims(text: str | None, terms: list[str], entities: list[str], max_claims: int = 3,
                   max_chars: int = 300) -> list[str]:
    """Attributed statements relevant to the event, verbatim."""
    if not text or max_claims <= 0:
        return []
    return [trim(s, max_chars) for s in _pick(text, terms, entities, max_claims, lambda s: bool(_ATTRIB.search(s)))]


def snippet_notes(snippet: str | None, title: str | None = None, max_chars: int = 300) -> list[EvidenceNote]:
    """A provider's own short summary as one note (when no page text may be used)."""
    if not snippet:
        return []
    s = " ".join(snippet.split())
    if title and s.lower().strip(" .") == title.lower().strip(" ."):
        return []
    return [EvidenceNote(text=trim(s, max_chars), kind="extract", source_field="snippet")]
