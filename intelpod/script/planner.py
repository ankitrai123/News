"""Story selection, ordering and fact sheets (deterministic).

Selection: a selected event becomes a story only if Phases 3-4 produced enough checked material
(at least two fact-sheet items, one of them a sourced statement). Fewer, deeper stories beat
many thin ones: the number of stories is capped by the word budget (`min_story_words` each).

Order: strongest story first (importance, plus debate, India relevance and evidenced links);
then a story with an evidenced connection to the one just placed comes next, so the episode
can move from one to the other on evidence rather than by listing.

Fact sheets: numbered items the dialogue must cite — verbatim confirmed/reported statements,
contested statements, debate positions, effect chains (sourced vs inference marked), India
statements, forecasts and uncertainties. Nothing is added that Phases 3-4 did not produce.
"""
from __future__ import annotations

import re

from ..debate.models import DebatePackage
from ..impact.models import EventLink, ImpactAnalysis
from ..research.models import ResearchResult
from ..schemas import RankedEvent
from .models import EpisodePlan, FactItem, StoryPlan

OPEN_CLOSE_WORDS = 300   # opening, what to watch, closing


class FactBook:
    def __init__(self):
        self.items: list[FactItem] = []

    def add(self, **kw) -> str:
        fid = f"F{len(self.items) + 1}"
        self.items.append(FactItem(fid=fid, **kw))
        return fid

    def get(self, fid: str) -> FactItem | None:
        i = int(fid[1:]) - 1 if fid[:1] == "F" and fid[1:].isdigit() else -1
        return self.items[i] if 0 <= i < len(self.items) else None


def _pubs(ids, sources: dict[str, ResearchResult]) -> list[str]:
    return list(dict.fromkeys(sources[i].publisher for i in ids if i in sources))


def story_facts(book: FactBook, r: RankedEvent, dp: DebatePackage | None, a: ImpactAnalysis | None,
                sources: dict[str, ResearchResult]) -> list[str]:
    fids: list[str] = []
    if dp is None or dp.status in ("skipped", "failed"):
        return fids
    claims = {c.claim_id: c for c in dp.claims}
    used: set[str] = set()

    def claim_item(cid: str, kind: str) -> None:
        c = claims.get(cid)
        if c is None or cid in used:
            return
        used.add(cid)
        fids.append(book.add(kind=kind, text=c.text, confidence=c.confidence, event_id=r.event_id,
                             claim_ids=[cid], source_ids=c.source_ids, publishers=_pubs(c.source_ids, sources)))

    for cid in dp.consensus[:5]:
        claim_item(cid, "fact")
    singles = [cid for cid in dp.unverified if claims.get(cid) and claims[cid].kind in ("factual", "attributed")]
    for cid in singles[:3]:
        claim_item(cid, "fact")
    for cid in dp.contested[:2]:
        claim_item(cid, "contested")
    for q in dp.questions[:2]:
        for p in q.positions:
            if not p.found:
                continue
            text = f"{p.label}: {p.summary or p.strongest_argument}".strip(": ")
            fids.append(book.add(kind="position", text=text, confidence=p.evidence_strength, event_id=r.event_id,
                                 source_ids=p.source_ids, publishers=_pubs(p.source_ids, sources),
                                 basis="quoted statements" if q.basis == "quoted statements" else "model, checked"))
    if a is not None:
        for ch in a.chains[:2]:
            steps = " → ".join(f"{s.text} [{'sourced' if s.basis == 'reported' else 'inference'}]" for s in ch.steps)
            cids = list(dict.fromkeys(c for s in ch.steps for c in s.claim_ids))
            srcs = list(dict.fromkeys(x for c in cids if c in claims for x in claims[c].source_ids))
            fids.append(book.add(kind="chain", text=f"{ch.title}: {steps}", confidence=ch.confidence,
                                 event_id=r.event_id, claim_ids=cids, source_ids=srcs, publishers=_pubs(srcs, sources),
                                 basis="inference" if any(s.basis == "inferred" for s in ch.steps) else "sourced"))
        if a.india:
            for cid in a.india.claim_ids[:2]:
                if cid in used:   # already a fact item: mark that it concerns India rather than repeating it
                    continue
                claim_item(cid, "india")
            if a.india_summary:
                fids.append(book.add(kind="india", text=a.india_summary, confidence="plausible", event_id=r.event_id,
                                     claim_ids=list(a.india.claim_ids[:3]), source_ids=a.india.source_ids,
                                     publishers=_pubs(a.india.source_ids, sources), basis="model, checked"))
    for cid in dp.forecasts[:2]:
        claim_item(cid, "forecast")
    # Phase 3's own tallies ("2 statement(s) are forecasts ...") are bookkeeping, not podcast material.
    for u in [x for x in dp.uncertainties if not re.match(r"^\d+ (?:well-supported )?statement", x)][:2]:
        fids.append(book.add(kind="uncertainty", text=u, confidence="speculative", event_id=r.event_id,
                             basis="IntelPod"))
    return fids


def plan_episode(book: FactBook, ranked: list[RankedEvent], debates: dict[str, DebatePackage],
                 impacts: dict[str, ImpactAnalysis], sources: dict[str, dict[str, ResearchResult]],
                 links: list[EventLink], cfg) -> EpisodePlan:
    plan = EpisodePlan(target_words=cfg.target_words)
    evidenced = [link for link in links if link.kind == "evidenced"]
    cands: list[tuple[float, StoryPlan]] = []
    for r in ranked:
        dp, a = debates.get(r.event_id), impacts.get(r.event_id)
        fids = story_facts(book, r, dp, a, sources.get(r.event_id, {}))
        # A story needs a statement from a retrieved source, not only model-written summaries.
        sourced = [f for f in fids if (it := book.get(f)) and it.kind in ("fact", "india", "contested")
                   and it.basis == "sourced" and it.claim_ids]
        if len(fids) < 2 or not sourced:
            plan.dropped.append(f"{r.title}: not enough checked material for a segment")
            continue
        linked = [link.event_b if link.event_a == r.event_id else link.event_a for link in evidenced
                  if r.event_id in (link.event_a, link.event_b)]
        india = bool(a and a.india)
        debated = bool(dp and dp.questions)
        score = r.importance + 10 * debated + 5 * india + 5 * bool(linked)
        why = [f"importance {r.importance:.0f}"] + (["sources disagree"] if debated else []) + \
              (["India relevance"] if india else []) + (["connected to another story"] if linked else [])
        cands.append((score, StoryPlan(event_id=r.event_id, title=r.title, rank=r.rank, facts=fids,
                                       connects_to=linked, india=india, why=", ".join(why))))
    if not cands:
        return plan
    n = max(1, min(cfg.max_stories, (cfg.target_words - OPEN_CLOSE_WORDS) // cfg.min_story_words))
    cands.sort(key=lambda x: -x[0])
    chosen = [c for _, c in cands[:n]]
    for _, c in cands[n:]:
        plan.dropped.append(f"{c.title}: left out to keep depth within the {cfg.target_words}-word target")
    ordered = [chosen.pop(0)]
    while chosen:
        last = ordered[-1]
        nxt = next((c for c in chosen if c.event_id in last.connects_to), chosen[0])
        if nxt.event_id in last.connects_to:
            nxt.why += "; placed after a connected story"
        chosen.remove(nxt)
        ordered.append(nxt)
    budget = max(0, cfg.target_words - OPEN_CLOSE_WORDS) // len(ordered)
    for s in ordered:
        # Never ask for more words than the material supports (~150 per item): padding invites invention.
        s.word_budget = max(80, min(budget, 150 * len(s.facts)))
    plan.stories = ordered
    return plan
