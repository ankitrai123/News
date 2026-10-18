"""Phase 4 orchestration: an intelligence brief for a run.

For each event with a debate package (best ranked first):
  1. context: what happened, what is new since the last run that covered it, timeline, earlier
     related events in IntelPod's database (context.py);
  2. impact evidence: dimensions, sectors and India channels the sources discuss; figures they
     state (tagging.py) — deterministic, always available;
  3. optional model analysis: causal chains, affected groups, positive/negative readings, India
     summary — every item checked against the claims it cites (analysis.py);
  4. forecast ledger: forecast claims are recorded with source, date and stated timeframe.
Then across events: connections (links.py), optionally worded by the model and checked.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from ..config import Settings
from ..debate.claims import FORECAST
from ..debate.models import Claim, DebatePackage
from ..research.model_assist import safe_call
from ..research.models import EventBrief, ResearchResult
from ..textutil import normalize_title, stable_hash
from . import analysis
from .context import build_context
from .links import explain_links, find_links
from .models import DIMENSIONS, Forecast, ImpactAnalysis, IntelligenceBrief
from .tagging import figures, tag_dimensions, timeframe

log = logging.getLogger(__name__)


class ImpactService:
    def __init__(self, settings: Settings, research_store, debate_store, store, llm_client=None,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.cfg = settings.impact
        self.rstore = research_store
        self.dstore = debate_store
        self.store = store
        self.llm = llm_client if self.cfg.model_analysis else None
        self.clock = clock
        self.model_calls = 0
        self.model_errors: list[dict] = []
        self._model_dead = llm_client is None
        self.notes: list[str] = []

    # -- run level ------------------------------------------------------------------------------
    def run(self, run_id: str, events: list[EventBrief], now: datetime, skip_done: bool = True) -> dict:
        t0 = self.clock()
        done = self.store.done_event_ids(run_id) if skip_done else set()
        per_event, new_forecasts = [], []
        for brief in events:
            if brief.event_id in done:
                per_event.append({"event_id": brief.event_id, "status": "already_done"})
                continue
            try:
                a, fids = self.analyse_event(run_id, brief, now)
                new_forecasts += fids
                per_event.append({"event_id": brief.event_id, "status": a.status, "chains": len(a.chains),
                                  "dimensions": len(a.dimensions), "figures": len(a.figures)})
            except Exception as e:  # noqa: BLE001 - one event must not stop the brief
                log.exception("impact analysis for %s failed", brief.event_id)
                self.store.save(ImpactAnalysis(run_id=run_id, event_id=brief.event_id, event_title=brief.title,
                                               status="failed", created_at=datetime.now(timezone.utc),
                                               limitations=[f"analysis failed: {type(e).__name__}: {str(e)[:200]}"]))
                per_event.append({"event_id": brief.event_id, "status": "failed", "error": str(e)[:300]})
        links, link_rejected = self.connect(run_id, events)
        limitations = list(dict.fromkeys(self.notes))
        if link_rejected:
            limitations.append(f"{len(link_rejected)} model link explanation(s) rejected as unsupported by the quotes.")
        self.store.save_brief(IntelligenceBrief(run_id=run_id, generated_for=now, event_ids=[b.event_id for b in events],
                                                links=links, new_forecasts=new_forecasts, limitations=limitations,
                                                created_at=datetime.now(timezone.utc)))
        return {"events": per_event, "links": len(links),
                "evidenced_links": sum(link.kind == "evidenced" for link in links), "new_forecasts": len(new_forecasts),
                "model_calls": self.model_calls, "model_errors": self.model_errors[:10],
                "seconds": round(self.clock() - t0, 1)}

    def connect(self, run_id: str, events: list[EventBrief]):
        pairs = []
        for b in events:
            dp = self.dstore.package(run_id, b.event_id)
            srcs = self._sources(run_id, b.event_id, dp)
            pairs.append((b, srcs))
        links = find_links(run_id, pairs)
        rejected: list[str] = []
        if links and self.cfg.explain_links and self._model_ok() and any(link.kind == "evidenced" for link in links):
            self.model_calls += 1
            res, err = safe_call(explain_links, self.llm, links)
            if err:
                self._model_error("links", err)
            else:
                rejected = res[1]
        self.store.save_links(run_id, links)
        return links, rejected

    # -- event level ------------------------------------------------------------------------------
    def _sources(self, run_id: str, event_id: str, dp: DebatePackage | None) -> list[ResearchResult]:
        results = self.rstore.results(run_id, event_id)
        if dp is not None and dp.source_ids:
            keep = set(dp.source_ids)
            return [r for r in results if r.result_id in keep]
        return [r for r in results if r.selected]

    def analyse_event(self, run_id: str, brief: EventBrief, now: datetime) -> tuple[ImpactAnalysis, list[str]]:
        dp = self.dstore.package(run_id, brief.event_id)
        sources = self._sources(run_id, brief.event_id, dp)
        claims = dp.claims if dp else []
        previous = self.store.previous_analysis(brief.event_id, run_id)
        prev_dp = self.dstore.package(previous.run_id, brief.event_id) if previous else None
        ctx = build_context(brief, dp, sources, self.store, previous, prev_dp.claims if prev_dp else None, now,
                            self.cfg.context_lookback_days)
        a = ImpactAnalysis(run_id=run_id, event_id=brief.event_id, event_title=brief.title, status="completed",
                           context=ctx, created_at=datetime.now(timezone.utc))
        if not claims:
            a.status = "skipped" if not sources else "partial"
            a.limitations.append("No claim map for this event (no sources, or Phase 3 did not run): impact is not "
                                 "assessed. Context comes from IntelPod's own records only.")
            self.store.save(a)
            return a, []
        a.dimensions, a.sectors, a.india = tag_dimensions(claims)
        a.not_discussed = [DIMENSIONS[k] for k in DIMENSIONS if k not in {d.key for d in a.dimensions}]
        a.figures = figures(claims)
        if a.india is None:
            a.limitations.append("No retrieved source discusses India in connection with this event; India "
                                 "implications are not assessed.")
        self._model_analysis(a, brief, claims, sources)
        fids = self.store.upsert_forecasts(self.forecasts(run_id, brief, claims, sources, now))
        self.store.save(a)
        return a, fids

    def _model_analysis(self, a: ImpactAnalysis, brief: EventBrief, claims: list[Claim],
                        sources: list[ResearchResult]) -> None:
        if not self.cfg.model_analysis:
            a.limitations.append("Model analysis is disabled (impact.model_analysis): causal chains and affected "
                                 "groups are not assessed; the evidence tables are complete.")
            return
        if not self._model_ok():
            reason = ("model call budget used up" if not self._model_dead else "no model available")
            a.status = "partial"
            a.limitations.append(f"Causal chains, affected groups and interpretations were not assessed ({reason}); "
                                 f"the evidence tables (dimensions, sectors, figures) are complete.")
            return
        publishers = {r.result_id: r.publisher for r in sources}
        self.model_calls += 1
        res, err = safe_call(analysis.analyse, self.llm, brief, claims, publishers)
        if err:
            self._model_error(brief.event_id, err)
            a.status = "partial"
            a.limitations.append(f"Model analysis failed ({err['error'][:160]}); the evidence tables are complete.")
            return
        out, ids, model = res
        all_text = " ".join(c.text for c in claims)
        a.analysed_by = model
        a.chains = analysis.to_chains(out, ids, all_text, a.rejected)
        a.actors = analysis.to_actors(out, ids, all_text, a.rejected)
        a.interpretations, a.interpretation_note = analysis.to_interpretations(out, ids, a.rejected)
        if out.what_happened and not analysis.unsupported_terms(out.what_happened, all_text):
            a.context.what_happened = out.what_happened
            a.context.what_happened_basis = f"summary by {model}, checked against the statements"
        elif out.what_happened:
            a.rejected.append("'what happened' summary named things not in the statements; lead statement kept")
        if out.india_summary:
            if a.india is None:
                a.rejected.append("India summary dropped: no retrieved statement concerns India")
            elif analysis.unsupported_terms(out.india_summary, all_text):
                a.rejected.append("India summary named things not in the statements; dropped")
            else:
                a.india_summary = out.india_summary
        if not a.chains:
            a.limitations.append("No causal chain could be tied to the retrieved statements.")
        if a.rejected:
            a.limitations.append(f"{len(a.rejected)} item(s) of model output were rejected (see 'rejected').")

    def forecasts(self, run_id: str, brief: EventBrief, claims: list[Claim], sources: list[ResearchResult],
                  now: datetime) -> list[Forecast]:
        by_id = {r.result_id: r for r in sources}
        out = []
        for c in claims:
            if c.kind != "forecast":
                continue
            src = next((by_id[s] for s in c.source_ids if s in by_id), None)
            m = FORECAST.search(c.text)
            out.append(Forecast(
                forecast_id=stable_hash("forecast", normalize_title(c.text)), text=c.text, event_id=brief.event_id,
                event_title=brief.title, first_run_id=run_id, last_run_id=run_id, first_seen_at=now,
                publisher=src.publisher if src else None, url=src.url if src else None, speaker=c.speaker,
                published_at=src.published_at if src else None, timeframe=timeframe(c.text),
                hedge=m.group(1) if m else None, source_ids=c.source_ids))
        return out

    def _model_ok(self) -> bool:
        if self._model_dead:
            return False
        if self.model_calls >= self.cfg.max_model_calls:
            self.notes.append(f"model call budget ({self.cfg.max_model_calls} per run) used up")
            return False
        return True

    def _model_error(self, where: str, err: dict) -> None:
        self.model_errors.append({"where": where, **err})
        if err.get("fatal"):
            self._model_dead = True
