"""Fact verification of script lines (Module 10), deterministic.

Each line is checked against the fact-sheet items it cites (which point at verbatim claims from
retrieved sources):

  citations   factual and uncertainty lines must cite items; cited ids must exist and belong to
              the story (opening / watch / closing lines may cite any story's items)
  names and   every capitalised name and every number must appear in the cited items; lines
  numbers     citing nothing may contain no number at all
  quotations  text in quotation marks must appear verbatim in the cited items
  hedging     a line resting on a forecast, a speculative item or an inference must sound like
              one ("could", "expected", ...); a contested item must be presented as disputed
  attribution a line resting only on single-source statements must name the source or say
              "reported"; fixed automatically ("According to Reuters, ...")
  causality   causal wording ("led to", "because", "will cause") needs a cited item that makes
              a causal link (an effect chain, or a statement using causal wording)

Outcome per line: supported, fixed (automatic, safe rewording), or a list of problems for one
model revision; lines that still fail are removed and listed in the evidence report.
"""
from __future__ import annotations

import re

from ..debate.analysis import unsupported_terms
from .models import FactItem, LineCheck, ScriptLine

HEDGE = re.compile(r"\b(could|may|might|would|expect(?:s|ed|ation)?|forecasts?|projected|likely|unlikely|possible|"
                   r"possibly|potential(?:ly)?|risk|risks|if|whether|predicts?|predicted|outlook|set to|"
                   r"poised to|plans?|aims?|hopes?|warns?|warned|fears?|uncertain|not yet|too early|remains to be seen|"
                   r"says? (?:it|they) will|said (?:it|they) would|according to)\b", re.I)
CONTEST = re.compile(r"\b(disput\w*|contest\w*|conflicting|differ\w*|disagree\w*|unclear|unconfirmed|claims?|claimed|"
                     r"alleg\w*|deni\w*|deny|reports? vary|not (?:yet )?(?:verified|confirmed)|accounts? differ)\b", re.I)
ATTRIBUTION = re.compile(r"\b(according to|reported|reports|reporting|said|says|told|claims?|cites?|citing|"
                         r"in an interview|wrote|writes|per)\b", re.I)
CAUSAL = re.compile(r"\b(because|caused|causes|cause|led to|leads to|lead to|leading to|result(?:s|ed)? in|due to|"
                    r"drove|drives|driven by|triggered|triggers|thanks to|as a result|therefore)\b", re.I)
QUOTE = re.compile(r"[\"“]([^\"”]{8,})[\"”]")
DIGIT = re.compile(r"\d")
NEEDS_CITES = ("fact", "uncertainty")


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("’", "'").replace("‘", "'").split())


def check_line(line: ScriptLine, facts: dict[str, FactItem], story_fids: set[str] | None,
               hosts: list[str], context: str = "") -> tuple[LineCheck, str | None]:
    """(check, fixed_text). `fixed_text` is set when an automatic fix makes the line pass."""
    problems: list[str] = []
    cited = []
    for fid in line.cites:
        it = facts.get(fid)
        if it is None:
            problems.append(f"cites unknown item {fid}")
        elif story_fids is not None and fid not in story_fids and it.kind != "link":
            problems.append(f"cites {fid}, which belongs to another story")
        else:
            cited.append(it)
    if line.kind in NEEDS_CITES and not cited:
        problems.append(f"a {line.kind} line must cite the fact sheet")
    if line.kind == "analysis" and not cited:
        problems.append("analysis must rest on a cited position, effect chain or statement")

    host_text = " ".join(hosts) + " IntelPod " + context   # context: the story / episode titles
    if cited:
        support = " ".join([it.text for it in cited] + [p for it in cited for p in it.publishers]) + " " + host_text
    else:
        pool = [facts[f] for f in (story_fids or facts.keys()) if f in facts]
        support = " ".join([it.text for it in pool] + [p for it in pool for p in it.publishers]) + " " + host_text
        if DIGIT.search(line.text):
            problems.append("a number in a line that cites nothing")
    bad = unsupported_terms(line.text, support)
    if bad:
        problems.append("names/numbers not in the cited items: " + ", ".join(bad[:5]))
    for q in QUOTE.findall(line.text):
        if _norm(q) not in _norm(" ".join(it.text for it in cited)):
            problems.append(f"quotation not found verbatim in the cited items: “{q[:60]}”")
    hedged = bool(HEDGE.search(line.text))
    if any(it.kind == "forecast" or it.confidence == "speculative" or it.basis == "inference" for it in cited) \
            and not hedged and line.kind != "question":
        problems.append("states a forecast, speculation or inference as fact (needs hedging)")
    if any(it.kind == "contested" or it.confidence == "contested" for it in cited) and not CONTEST.search(line.text) \
            and line.kind != "question":
        problems.append("presents a disputed statement without saying it is disputed")
    if CAUSAL.search(line.text) and line.kind != "question":
        if not any(it.kind == "chain" or CAUSAL.search(it.text) for it in cited):
            problems.append("causal claim goes beyond the cited evidence")

    fixed = None
    single = [it for it in cited if it.kind in ("fact", "india") and it.basis == "sourced"]
    if line.kind == "fact" and single and all(it.confidence == "reported" for it in single) \
            and not any(it.confidence == "confirmed" for it in cited):
        pubs = [p for it in single for p in it.publishers]
        if not ATTRIBUTION.search(line.text) and not any(p.lower() in line.text.lower() for p in pubs):
            if pubs and not problems:
                body = line.text[0].lower() + line.text[1:] if line.text[:2].istitle() and not line.text[1:2].isupper() \
                    else line.text
                fixed = f"According to {pubs[0]}, {body}"
            else:
                problems.append("a single-source statement must be attributed (\"according to ...\")")
    if problems:
        return LineCheck(status="unchecked", problems=problems), None
    if fixed:
        return LineCheck(status="fixed", problems=["single-source statement: attribution added"],
                         original=line.text), fixed
    return LineCheck(status="supported"), None
