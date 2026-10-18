"""Disagreement between named actors, found without a model.

When the sources quote named people or bodies explicitly backing the event's subject ("Rubio
defended the waiver") and others explicitly criticising it ("Zelenskyy called the waiver
wrong"), that IS a disagreement the briefing must show, whatever the model says.

Deterministic and conservative:
  * only attributed statements (or value judgements) with an identified speaker count;
  * stance comes from explicit wording (defend/welcome/praise vs criticise/condemn/wrong/illegal);
  * the statement must be about this event (shares a distinctive word with its title or a
    named entity of the event);
  * both sides need at least one statement, from different speakers;
  * positions quote the statements verbatim; nothing is summarised or invented.
"""
from __future__ import annotations

import re

from ..research.models import EventBrief, ResearchResult
from ..textutil import STOPWORDS
from .analysis import evidence_strength
from .models import Claim, Position, Question

SUPPORT = re.compile(r"\b(defend(?:s|ed|ing)?|welcom(?:es|ed|ing)|prais(?:es|ed|ing)|back(?:s|ed|ing) the|"
                     r"endors(?:es|ed|ing)|hail(?:s|ed|ing)|laud(?:s|ed|ing)|applaud(?:s|ed|ing)|"
                     r"justif(?:y|ies|ied)|support(?:s|ed|ing)? the|called it (?:a )?(?:necessary|right|good|"
                     r"historic|important|positive)|(?:is|was) (?:necessary|the right (?:move|decision|call)))\b", re.I)
OPPOSE = re.compile(r"\b(criticis(?:es|ed|ing)|criticiz(?:es|ed|ing)|condemn(?:s|ed|ing)|slam(?:s|med|ming)|"
                    r"oppos(?:es|ed|ing)|denounc(?:es|ed|ing)|blast(?:s|ed|ing)|attack(?:s|ed) the|"
                    r"wrong|illegal|unlawful|unconstitutional|violat(?:es|ed|ing|ion)|breaks? (?:the |a )?\w*\s?law|"
                    r"mistake|betrayal|reckless|dangerous|unacceptable|warn(?:s|ed)? against|object(?:s|ed) to|"
                    r"called (?:for )?(?:it|the \w+) (?:to be )?(?:scrapped|reversed|withdrawn))\b", re.I)
_WORD = re.compile(r"[a-z][a-z\-]{3,}")


def _subject_terms(brief: EventBrief) -> set[str]:
    words = {w for w in _WORD.findall((brief.headline or brief.title).lower()) if w not in STOPWORDS}
    return words | {e.lower() for e in brief.entities if len(e) >= 4}


def stance(text: str) -> str | None:
    sup, opp = bool(SUPPORT.search(text)), bool(OPPOSE.search(text))
    if sup == opp:
        return None   # neither, or both (e.g. "defended ... against criticism"): unclear, not counted
    return "support" if sup else "oppose"


def stated_positions(brief: EventBrief, claims: list[Claim], sources: list[ResearchResult]) -> Question | None:
    terms = _subject_terms(brief)
    by_id = {r.result_id: r for r in sources}
    sides: dict[str, list[Claim]] = {"support": [], "oppose": []}
    for c in claims:
        if c.kind not in ("attributed", "value_judgement") or not c.speaker:
            continue
        low = c.text.lower()
        if not any(t in low for t in terms):
            continue
        st = stance(c.text)
        if st:
            sides[st].append(c)
    if not sides["support"] or not sides["oppose"]:
        return None
    speakers = {st: {c.speaker.lower() for c in cs} for st, cs in sides.items()}
    if speakers["support"] == speakers["oppose"]:
        return None   # one actor on both sides is not a disagreement between actors
    positions = []
    for st, label in (("support", "Backed or defended by"), ("oppose", "Criticised or opposed by")):
        cs = sorted(sides[st], key=lambda c: (-c.independent_sources, -len(c.text)))
        names = list(dict.fromkeys(c.speaker for c in cs))
        srcs = [by_id[s] for c in cs for s in c.source_ids if s in by_id]
        strength, why = evidence_strength(list({r.result_id: r for r in srcs}.values()))
        positions.append(Position(
            label=f"{label} {', '.join(names[:4])}", summary=" / ".join(f"“{c.text}”" for c in cs[:2]),
            strongest_argument=cs[0].text, source_ids=list(dict.fromkeys(r.result_id for r in srcs)),
            evidence_strength=strength, strength_reason=f"{why}; quoted statements",
            stakeholders=sorted({c.stakeholder for c in cs if c.stakeholder}), found=True))
    return Question(question="Named actors quoted in the sources take opposing positions on this event",
                    disagreement_type="value", positions=positions, basis="quoted statements")
