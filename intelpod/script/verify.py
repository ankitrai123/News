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

# Hedges must actually qualify the statement: attribution ("according to") or incidental words
# ("if", "risk", "plans") do not turn a forecast into a possibility.
HEDGE = re.compile(r"\b(could|may|might|would|expect(?:s|ed|ation)?|forecasts?|forecast to|projected|likely|unlikely|"
                   r"possible|possibly|potential(?:ly)?|predicts?|predicted|outlook|set to|poised to|uncertain|"
                   r"not yet|too early|remains to be seen|is seen as|are seen as)\b", re.I)
CONTEST = re.compile(r"\b(disput\w*|contested|contests|conflicting|disagree\w*|unclear|unconfirmed|alleg\w*|denie[sd]|"
                     r"deny|denying|reports? vary|not (?:yet )?(?:verified|confirmed)|accounts? differ|differ(?:s|ed)?)\b",
                     re.I)
# Attribution: a publisher name, or explicit reporting language. "said"/"per" alone are not enough.
ATTRIBUTION = re.compile(r"\b(according to|reported|reports|reporting)\b", re.I)
CAUSAL = re.compile(r"\b(because|caused|causes|cause|led to|leads to|lead to|leading to|result(?:s|ed)? in|due to|"
                    r"drove|drives|driven by|triggered|triggers|thanks to|as a result|therefore|sparked|sparks|"
                    r"fuel(?:l)?ed|fuels|prompted|prompts|spurred|spurs|forced|forces|boosted|boosts|pushed|pushes|"
                    r"means that|meant that|hence|consequently)\b", re.I)
# Quotations in any common style; a single-quoted span must look like a quote, not an apostrophe.
QUOTES = [re.compile(r"[\"“]([^\"”]{8,})[\"”]"), re.compile(r"‘([^’]{8,})’"), re.compile(r"«\s*([^»]{8,}?)\s*»"),
          re.compile(r"(?:(?<=\s)|^)'([^']{8,}?)'(?=[\s.,;:!?]|$)")]
DIGIT = re.compile(r"\d")
NUMBER_WORDS = re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                          r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
                          r"hundreds?|thousands?|millions?|billions?|trillions?|crores?|lakhs?|dozens?|half|halved|"
                          r"third|thirds|quarter of|double[sd]?|triple[sd]?|twice|thrice|percent|per cent)\b", re.I)
TIME_WORDS = re.compile(r"\b(yesterday|today|tomorrow|tonight|last (?:week|month|year|quarter)|next (?:week|month|year|"
                        r"quarter)|this (?:week|month|year|quarter)|monday|tuesday|wednesday|thursday|friday|saturday|"
                        r"sunday|january|february|march|april|june|july|august|september|october|november|"
                        r"december)\b", re.I)
NEGATION = re.compile(r"\b(not|no|never|neither|nor|without)\b|n't\b", re.I)
PREP_NUM = re.compile(r"\b(by|to|from|at|of)\s+(\$|₹|€|£)?(\d[\d,]*(?:\.\d+)?)", re.I)
NEEDS_CITES = ("fact", "uncertainty")
QUOTABLE = ("sourced", "quoted statements")


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("’", "'").replace("‘", "'").split())


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z'\-]+", s.lower()))


def check_line(line: ScriptLine, facts: dict[str, FactItem], story_fids: set[str] | None,
               hosts: list[str], context: str = "") -> tuple[LineCheck, str | None]:
    """(check, fixed_text). `fixed_text` is set when an automatic fix makes the line pass.

    `story_fids` limits which items the line may cite (the story's, or for opening / watch /
    closing the planned stories'). `context` is the line's own story title: it may support names
    in transition and opening lines, never numbers."""
    problems: list[str] = []
    kind = line.kind
    if kind == "question" and not line.text.rstrip().endswith("?"):
        kind = "fact"   # a "question" that asserts something is checked as an assertion
    cited = []
    for fid in line.cites:
        it = facts.get(fid)
        if it is None:
            problems.append(f"cites unknown item {fid}")
        elif story_fids is not None and fid not in story_fids:
            problems.append(f"cites {fid}, which is not part of this segment")
        else:
            cited.append(it)
    if line.segment == "watch" and cited and not any(it.kind in ("forecast", "uncertainty") for it in cited):
        problems.append("'what to watch' must rest on a forecast or open question")
    if kind in NEEDS_CITES and not cited:
        problems.append(f"a {kind} line must cite the fact sheet")
    if kind == "analysis" and not cited:
        problems.append("analysis must rest on a cited position, effect chain or statement")

    item_text = " ".join(it.text for it in cited)
    host_text = " ".join(hosts) + " IntelPod"
    support = " ".join([item_text] + [p for it in cited for p in it.publishers] + [host_text])
    if not cited and DIGIT.search(line.text):
        problems.append("a number in a line that cites nothing")
    bad = unsupported_terms(line.text, support)
    if bad and context and (kind == "transition" or line.segment == "opening"):
        title_words = _words(context)
        bad = [b for b in bad if any(ch.isdigit() for ch in b) or not all(w in title_words for w in _words(b))]
    if bad:
        problems.append("names/numbers not in the cited items: " + ", ".join(bad[:5]))
    if kind in ("fact", "analysis", "uncertainty") and cited:
        low_items = item_text.lower()
        words = [w for w in NUMBER_WORDS.findall(line.text) if w.lower() not in low_items]
        if words:
            problems.append("quantities in words not in the cited items: " + ", ".join(dict.fromkeys(words)))
        times = [w for w in TIME_WORDS.findall(line.text) if w.lower() not in low_items]
        if times:
            problems.append("dates or times not in the cited items: " + ", ".join(dict.fromkeys(times)))
        if NEGATION.search(line.text) and not NEGATION.search(item_text):
            problems.append("negation not in the cited items")
        item_preps = {(m.group(1).lower(), m.group(3)) for m in PREP_NUM.finditer(item_text)}
        item_nums = {num for _, num in item_preps}
        for m in PREP_NUM.finditer(line.text):
            if m.group(3) in item_nums and (m.group(1).lower(), m.group(3)) not in item_preps:
                problems.append(f"figure {m.group(3)} used differently from the cited items ('{m.group(1)} {m.group(3)}')")
    quotable = " ".join(it.text for it in cited if it.basis in QUOTABLE)
    for rx in QUOTES:
        for q in rx.findall(line.text):
            if _norm(q) not in _norm(quotable):
                problems.append(f"quotation not found verbatim in a cited source statement: “{q[:60]}”")
    if kind != "question":
        if any(it.kind == "forecast" or it.confidence == "speculative" or it.basis == "inference" for it in cited) \
                and not HEDGE.search(line.text):
            problems.append("states a forecast, speculation or inference as fact (needs hedging)")
        if any(it.kind == "contested" or it.confidence == "contested" for it in cited) and not CONTEST.search(line.text):
            problems.append("presents a disputed statement without saying it is disputed")
        if CAUSAL.search(line.text) and not any(it.kind == "chain" or CAUSAL.search(it.text) for it in cited):
            problems.append("causal claim goes beyond the cited evidence")

    fixed = None
    single = [it for it in cited if it.kind in ("fact", "india") and it.basis == "sourced"]
    if single and all(it.confidence == "reported" for it in single) and not any(it.confidence == "confirmed" for it in cited):
        pubs = [p for it in single for p in it.publishers]
        if not ATTRIBUTION.search(line.text) and not any(p.lower() in line.text.lower() for p in pubs):
            if pubs and not problems:
                first = line.text.split()[0] if line.text.split() else ""
                from ..debate.analysis import GROUP_WORDS, LEAD_WORDS
                from ..textutil import STOPWORDS
                lw = first.lower().strip(",.;:")
                lower_ok = (lw in LEAD_WORDS or lw in STOPWORDS or lw in GROUP_WORDS
                            or bool(re.search(r"(?<![A-Za-z])" + re.escape(lw) + r"(?![A-Za-z])", item_text)))
                body = line.text[0].lower() + line.text[1:] if lower_ok else line.text
                fixed = f"According to {pubs[0]}, {body}"
            else:
                problems.append("a single-source statement must be attributed (\"according to ...\")")
    if problems:
        return LineCheck(status="unchecked", problems=list(dict.fromkeys(problems))), None
    if fixed:
        return LineCheck(status="fixed", problems=["single-source statement: attribution added"],
                         original=line.text), fixed
    return LineCheck(status="supported"), None
