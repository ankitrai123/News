"""Phase 3 orchestration: turn an event's research package into a debate package.

For each researched event (best ranked first):

 1. claim map: verbatim statements from the selected sources, typed, grouped across sources,
    contradictions linked, confidence calibrated (claims.py);
 2. diversity: who is represented and what is missing (diversity.py, registry facts only);
 3. model analysis (optional): interpretive questions, positions, uncertainties, and missing
    perspectives with search keywords (analysis.py);
 4. viewpoint searches for the gaps and missing perspectives, through Phase 2's own pipeline
    (robots, paywalls, relevance, quality); new sources join the research package;
 5. if sources were added: claims and diversity are rebuilt and the model re-asked;
 6. IntelPod validates everything the model said (source ids, names and numbers grounded in the
    cited sources, at least two evidenced positions per question), computes evidence strength
    itself, sorts claims into consensus / contested / unverified / forecasts, rates overall
    confidence, and records every limitation — including perspectives it looked for and did not find.

Nothing here invents a source or a viewpoint; "no meaningful disagreement" is a valid result.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..config import Settings
from ..research.models import EventBrief, ResearchQuery, ResearchResult
from ..research.providers import gdelt_query_problems
from ..research.queries import (CUE_WORDS, OFFICIAL_DOMAINS, GLOBAL_OFFICIAL, Keywords, _fit, _or, clean_keyword,
                                event_keywords, gdelt_core)
from ..research.model_assist import safe_call
from ..schemas import Article
from ..textutil import Taxonomy, stable_hash
from . import analysis
from .claims import STAKEHOLDERS, build_claims
from .stances import stated_positions
from .diversity import ECON_TOPICS, diversity_report, source_country
from .models import Claim, DebatePackage, DiversityReport, ViewpointSearch
from .registry import Registry, country_code

log = logging.getLogger(__name__)

STAKEHOLDER_CUES = {
    "government": ["minister", "ministry", "officials", "government"],
    "expert": ["economists", "analysts", "experts", "think tank"],
    "corporate": ["companies", "industry", "executives", "exporters"],
    "investor": ["investors", "markets", "traders", "analysts"],
    "labour": ["unions", "workers"],
    "consumer": ["consumers", "households"],
}


class DebateService:
    def __init__(self, settings: Settings, research_store, debate_store, registry: Registry, taxonomy: Taxonomy,
                 research_service=None, llm_client=None, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.cfg = settings.debate
        self.rstore = research_store
        self.store = debate_store
        self.registry = registry
        self.taxonomy = taxonomy
        self.research = research_service      # for viewpoint searches; None = no searching
        self.llm = llm_client if self.cfg.model_analysis else None
        self.clock = clock
        self.model_calls = 0
        self.model_errors: list[dict] = []
        self._model_dead = self.llm is None
        self.searches = 0
        self.sources_added = 0

    # -- run level ------------------------------------------------------------------------
    def debate_events(self, run_id: str, events: list[tuple[EventBrief, list[Article]]], now: datetime,
                      skip_done: bool = True) -> dict:
        t0 = self.clock()
        done = self.store.done_event_ids(run_id) if skip_done else set()
        per_event = []
        for brief, articles in events:
            if brief.event_id in done:
                per_event.append({"event_id": brief.event_id, "status": "already_done"})
                continue
            try:
                pkg = self.debate_event(run_id, brief, articles, now)
                per_event.append({"event_id": brief.event_id, "status": pkg.status, "claims": len(pkg.claims),
                                  "questions": len(pkg.questions), "sources": len(pkg.source_ids),
                                  "confidence": pkg.overall_confidence})
            except Exception as e:  # noqa: BLE001 - one event's failure must not stop the others
                log.exception("debate analysis for %s failed", brief.event_id)
                self.store.save(DebatePackage(run_id=run_id, event_id=brief.event_id, event_title=brief.title,
                                              status="failed", limitations=[f"analysis failed: {type(e).__name__}: "
                                                                            f"{str(e)[:200]}"],
                                              created_at=datetime.now(timezone.utc)))
                per_event.append({"event_id": brief.event_id, "status": "failed", "error": str(e)[:300]})
        return {"events": per_event, "model_calls": self.model_calls, "model_errors": self.model_errors[:10],
                "viewpoint_searches": self.searches, "sources_added": self.sources_added,
                "seconds": round(self.clock() - t0, 1)}

    # -- event level ----------------------------------------------------------------------
    def debate_event(self, run_id: str, brief: EventBrief, articles: list[Article], now: datetime) -> DebatePackage:
        rpkg = self.rstore.package(run_id, brief.event_id)
        sources = self.rstore.results(run_id, brief.event_id, selected_only=True)
        if not sources:
            why = (rpkg.limitations[0] if rpkg and rpkg.limitations else "research found no usable sources")
            pkg = DebatePackage(run_id=run_id, event_id=brief.event_id, event_title=brief.title, status="skipped",
                                limitations=[f"No retrieved sources to compare ({why}). No claims, viewpoints or "
                                             f"debate are presented for this event."],
                                created_at=datetime.now(timezone.utc))
            self.store.save(pkg)
            return pkg
        sources = [self._with_registry_type(r) for r in sources]
        rejected: list[str] = []
        model_notes: list[str] = []
        claims = build_claims(sources)
        div = diversity_report(brief, sources, claims, self.registry)
        out, ids, model = self._analyse(brief, sources, model_notes)

        searches = self._viewpoint_searches(run_id, brief, articles, now, sources, div, out)
        added = [self._with_registry_type(r) for s in searches for r in s.added]
        if added:
            sources = sources + added
            claims = build_claims(sources)
            div = diversity_report(brief, sources, claims, self.registry)
            out2, ids2, model2 = self._analyse(brief, sources, model_notes)
            if out2 is not None:
                out, ids, model = out2, ids2, model2
        vsearch = [s.record for s in searches]

        stakeholders: dict[str, list[str]] = {}
        for c in claims:
            if c.stakeholder:
                for sid in c.source_ids:
                    stakeholders.setdefault(sid, []).append(c.stakeholder)
        all_text = " ".join(analysis.source_text(s) for s in sources)
        questions, uncertainties, would_change = [], [], []
        if out is not None:
            questions, rej = analysis.to_questions(out, ids, stakeholders)
            rejected += rej
            uncertainties = analysis.grounded_list(out.uncertainties, all_text, "uncertainty", rejected)
            would_change = analysis.grounded_list(out.would_change, all_text, "'would change' item", rejected)

        # Actors quoted on opposite sides (deterministic, verbatim) are a disagreement even when the
        # model found none or is unavailable.
        quoted = stated_positions(brief, claims, sources)
        if quoted is not None and len(questions) < 3:
            questions.append(quoted)
        by_kind = sort_claims(claims)
        uncertainties = claim_uncertainties(claims) + uncertainties
        note = no_disagreement_note(out is not None, questions, by_kind["contested"], len(sources),
                                    proposed=bool(out is not None and out.questions))
        level, why = overall_confidence(claims, sources, div, by_kind)
        limitations = self._limitations(div, vsearch, model_notes, rejected, out is not None, rpkg)
        pkg = DebatePackage(
            run_id=run_id, event_id=brief.event_id, event_title=brief.title,
            status="completed" if (out is not None or not self.cfg.model_analysis) else "partial",
            source_ids=[s.result_id for s in sources], claims=claims, consensus=by_kind["consensus"],
            contested=by_kind["contested"], unverified=by_kind["unverified"], forecasts=by_kind["forecasts"],
            questions=questions, no_disagreement_note=note, uncertainties=uncertainties[:8],
            would_change=would_change, diversity=div, viewpoint_searches=vsearch, overall_confidence=level,
            confidence_reason=why, analysed_by=model if out is not None else None, rejected=rejected,
            limitations=limitations, created_at=datetime.now(timezone.utc))
        self.store.save(pkg)
        return pkg

    def _with_registry_type(self, r: ResearchResult) -> ResearchResult:
        """GDELT labels every site "news"; the registry knows that rbi.org.in is official."""
        prof = self.registry.lookup(r.domain, r.publisher)
        if prof and prof.source_type in ("official", "international_org", "think_tank", "academic") \
                and r.source_type in (None, "news", "unknown"):
            return r.model_copy(update={"source_type": prof.source_type})
        return r

    # -- model ------------------------------------------------------------------------------
    def _analyse(self, brief: EventBrief, sources: list[ResearchResult], notes: list[str]):
        if self._model_dead:
            return None, {}, None
        if self.model_calls >= self.cfg.max_model_calls:
            notes.append(f"model analysis budget ({self.cfg.max_model_calls} calls per run) used up")
            return None, {}, None
        top = sorted(sources, key=lambda r: -r.relevance_score)[: self.cfg.max_sources_to_model]
        countries = {r.result_id: source_country(r, self.registry) for r in top}
        self.model_calls += 1
        res, err = safe_call(analysis.analyse, self.llm, brief, top, countries)
        if err:
            self.model_errors.append({"event_id": brief.event_id, **err})
            notes.append(f"model analysis failed: {err['error'][:160]}")
            if err.get("fatal"):
                self._model_dead = True
            return None, {}, None
        return res

    # -- viewpoint searches -----------------------------------------------------------------
    def _viewpoint_searches(self, run_id, brief, articles, now, sources, div: DiversityReport, out) -> list["_Search"]:
        if self.research is None or self.cfg.viewpoint_searches_per_event <= 0:
            return []
        plans = plan_searches(brief, self.taxonomy, div, out, now, self.settings.research.window_days,
                              list(self.research.providers), self.settings.research.gdelt.language_filter)
        done: list[_Search] = []
        existing = list(sources)
        for perspective, reason, queries, check in plans[: self.cfg.viewpoint_searches_per_event]:
            self.searches += 1
            try:
                added = self.research.supplement(run_id, brief, articles, queries, now, existing, reason,
                                                 limit=self.cfg.sources_added_per_search)
            except Exception as e:  # noqa: BLE001 - a failed search is a limitation, not a crash
                log.exception("viewpoint search failed")
                added = []
                reason = f"{reason} (search failed: {type(e).__name__})"
            existing += added
            self.sources_added += len(added)
            matching = [r for r in added if check(self._with_registry_type(r), self.registry)]
            rec = ViewpointSearch(perspective=perspective, reason=reason, queries=[q.text for q in queries],
                                  added_source_ids=[r.result_id for r in added], found=bool(matching))
            done.append(_Search(rec, added))
        return done

    def _limitations(self, div: DiversityReport, searches: list[ViewpointSearch], model_notes: list[str],
                     rejected: list[str], analysed: bool, rpkg) -> list[str]:
        out = []
        if div.sources < 3:
            out.append(f"Only {div.sources} source(s) were retrieved; agreement and disagreement cannot be judged "
                       f"reliably.")
        unsearched = [g for g in div.gaps if not g.startswith("Editorial orientation")]
        if unsearched and not searches:
            out.append(f"{len(unsearched)} coverage gap(s) remain and were not searched for (see 'Who is represented').")
        for s in searches:
            if not s.found:
                extra = (f" ({len(s.added_source_ids)} other relevant source(s) were added, but none from this "
                         f"perspective)") if s.added_source_ids else ""
                out.append(f"Looked for {s.perspective} ({s.reason}); no relevant, accessible source was found{extra}.")
        if not analysed:
            if not self.cfg.model_analysis:
                out.append("Model analysis is disabled (debate.model_analysis): interpretive questions and "
                           "positions were not assessed; the claim map is complete.")
            elif self.llm is None:
                out.append("No model available: interpretive questions and positions were not assessed; the claim "
                           "map (agreement, contradictions, single-source claims) is complete.")
        out += list(dict.fromkeys(model_notes))
        if rejected:
            out.append(f"{len(rejected)} item(s) of model output were rejected because they were not supported by "
                       f"the retrieved sources (listed under 'rejected').")
        if rpkg is not None and rpkg.status == "partial":
            out.append("The research for this event was partial (see its research limitations).")
        return out


class _Search:
    def __init__(self, record: ViewpointSearch, added: list[ResearchResult]):
        self.record = record
        self.added = added


# -- planning -------------------------------------------------------------------------------
# GDELT's sourcecountry operator takes FIPS 10-4 codes (docs/PROVIDERS.md), which differ from ISO.
ISO_TO_FIPS = {
    "US": "US", "GB": "UK", "IN": "IN", "CN": "CH", "RU": "RS", "JP": "JA", "DE": "GM", "FR": "FR", "IT": "IT",
    "CA": "CA", "AU": "AS", "KR": "KS", "SG": "SN", "HK": "HK", "TW": "TW", "BR": "BR", "MX": "MX", "ID": "ID",
    "TR": "TU", "SA": "SA", "AE": "AE", "QA": "QA", "IL": "IS", "IR": "IR", "PK": "PK", "BD": "BG", "ZA": "SF",
    "NG": "NI", "KE": "KE", "EG": "EG", "UA": "UP", "PL": "PL", "ES": "SP", "NL": "NL", "CH": "SZ", "SE": "SW",
    "NO": "NO", "IE": "EI", "BE": "BE", "PH": "RP", "VN": "VM", "TH": "TH", "MY": "MY", "AR": "AR", "NZ": "NZ",
    "LK": "CE", "NP": "NP",
}
STAKE_RX = {name: rx for name, rx in STAKEHOLDERS}


def _from_country(code: str):
    return lambda r, reg: source_country(r, reg) == code


def _is_primary(r: ResearchResult, reg) -> bool:
    return r.content_type == "primary" or r.source_type in ("official", "international_org")


def _is_analysis(r: ResearchResult, reg) -> bool:
    return r.content_type in ("analysis", "opinion", "research")


def _quotes(stake: str):
    rx = STAKE_RX.get(stake)
    return lambda r, reg: bool(rx and rx.search(" ".join([r.title] + [n.text for n in r.evidence_notes] + r.key_claims)))


def _any(r, reg) -> bool:
    return True


def plan_searches(brief: EventBrief, taxonomy: Taxonomy, div: DiversityReport, out, now: datetime,
                  window_days: int, providers: list[str], lang: str = "sourcelang:english") -> list:
    """(perspective, reason, queries, check) for the gaps in this event's sources, most important
    first. `check(result, registry)` says whether an added source really is that perspective.
    Every search is a normal provider query; nothing is assumed to exist."""
    kw = event_keywords(brief, taxonomy)
    base_terms = list(dict.fromkeys(kw.anchors() + kw.terms[:4]))
    start = now - timedelta(days=window_days)
    # perspective, reason, query type, GDELT filter part, local terms, GDELT only?, check
    plans: list[tuple] = []

    present = {k for k in div.countries if k != "unknown"}
    for c in brief.countries:
        code = country_code(c)
        if code and code not in present and code in ISO_TO_FIPS:
            plans.append((f"sources based in {c}", f"no source from {c}, a party to this event", "political_views",
                          f"sourcecountry:{ISO_TO_FIPS[code]}", [], True, _from_country(code)))
    wanted = ["government", "expert"] + (["corporate", "investor"] if brief.topic in ECON_TOPICS else [])
    missing_stake = [g for g in wanted if g not in div.stakeholders]
    for g in div.gaps:
        if g.startswith("No quoted voices from"):
            for st in missing_stake[:2]:
                cues = STAKEHOLDER_CUES[st]
                plans.append((f"{st} voices", f"no quoted {st} voices among the sources",
                              "business_impact" if st in ("corporate", "investor") else "political_views",
                              _or(cues), base_terms + cues[:2], False, _quotes(st)))
        elif g.startswith("No analysis"):
            plans.append(("analysis and commentary", "only straight reporting was found", "expert_analysis",
                          _or(CUE_WORDS["expert_analysis"][:4]), base_terms + ["analysis"], False, _is_analysis))
        elif g.startswith("No primary source"):
            domains = [d for c in brief.countries for d in OFFICIAL_DOMAINS.get(c, [])][:4] or GLOBAL_OFFICIAL[:4]
            plans.append(("official statements", "no primary source (official statement or document)",
                          "primary_sources", _or([f"domainis:{d}" for d in domains]), [], True, _is_primary))
    if out is not None:
        for p in out.missing_perspectives[:3]:
            words = [k for x in p.keywords[:4] if (k := clean_keyword(x))][:3]
            label = " ".join(p.perspective.split())[:60]
            if words and label:
                plans.insert(len(plans) // 2 + 1, (label, "suggested by the model as a missing perspective",
                                                   "criticism", _or(words), base_terms + words, False, _any))
    if not plans:  # Phase 2 already tried criticism; once more only if nothing else is planned
        plans.append(("criticism and counterarguments", "check for dissenting views", "criticism",
                      _or(CUE_WORDS["criticism"][:4]), base_terms + ["criticism"], False, _any))

    result = []
    seen = set()
    for perspective, reason, qtype, part, terms, gdelt_only, check in plans:
        if perspective.lower() in seen:
            continue
        seen.add(perspective.lower())
        queries = []
        for prov in providers:
            if prov == "gdelt":
                text = None
                for n in (3, 2, 1):  # fewer anchors until the perspective part fits
                    kw_n = Keywords(phrases=kw.phrases, entities=kw.entities, countries=kw.countries,
                                    terms=kw.terms, topic=kw.topic)
                    core = _core_with(kw_n, n)
                    if not core:
                        break
                    lang_part = "" if part.startswith("sourcecountry:") else lang
                    cand = _fit([core, part], lang_part)
                    if part in cand and not gdelt_query_problems(cand):
                        text = cand
                        break
                if not text:
                    continue
            elif gdelt_only:
                continue  # local search and the Guardian cannot filter by country or site
            elif prov in ("archive", "feeds"):
                text = ", ".join(dict.fromkeys(terms))
            elif prov == "guardian":
                anchors = kw.anchors() or kw.terms[:3]
                if not anchors:
                    continue
                text = f"{_or(anchors)} AND {part}"
            else:
                continue
            qid = stable_hash(brief.event_id, "viewpoint", prov, text, start.isoformat(), now.isoformat())
            queries.append(ResearchQuery(query_id=qid, event_id=brief.event_id, query_type=qtype, provider=prov,
                                         text=text, terms=list(dict.fromkeys(terms or base_terms)), start=start,
                                         end=now, origin="template"))
        if queries:
            result.append((perspective, reason, queries, check))
    return result


def _core_with(kw: Keywords, n_anchors: int) -> str:
    """The GDELT event core using at most `n_anchors` names/phrases."""
    anchors = kw.anchors(n_anchors)
    if anchors:
        kw = Keywords(phrases=[a for a in kw.phrases if a in anchors], entities=[a for a in kw.entities if a in anchors],
                      countries=kw.countries, terms=kw.terms, topic=kw.topic)
    return gdelt_core(kw)


# -- summaries ------------------------------------------------------------------------------
def sort_claims(claims: list[Claim]) -> dict[str, list[str]]:
    out = {"consensus": [], "contested": [], "unverified": [], "forecasts": []}
    for c in claims:
        if c.confidence == "contested":
            out["contested"].append(c.claim_id)
        elif c.kind == "forecast":
            out["forecasts"].append(c.claim_id)
        elif c.confidence == "confirmed":
            out["consensus"].append(c.claim_id)
        elif c.kind in ("factual", "attributed") and c.independent_sources <= 1:
            out["unverified"].append(c.claim_id)
    return out


def claim_uncertainties(claims: list[Claim]) -> list[str]:
    out = []
    contested = [c for c in claims if c.confidence == "contested"]
    if contested:
        out.append(f"{len(contested)} statement(s) are contradicted by other sources (see contested claims).")
    disputed_confirmed = [c for c in claims if c.confidence == "confirmed" and c.contradicted_by]
    if disputed_confirmed:
        out.append(f"{len(disputed_confirmed)} well-supported statement(s) are reported differently by a minority "
                   f"of sources.")
    forecasts = [c for c in claims if c.kind == "forecast"]
    if forecasts:
        out.append(f"{len(forecasts)} statement(s) are forecasts or expectations and cannot be verified yet.")
    return out


def no_disagreement_note(analysed: bool, questions, contested: list[str], n_sources: int,
                         proposed: bool = False) -> str:
    if questions:
        return ""
    if proposed:
        return ("The model suggested possible disagreements, but none could be verified against the retrieved "
                "sources (see 'rejected'); IntelPod does not present them as a debate.")
    if not analysed:
        if contested:
            return ""
        return ("Interpretive disagreement was not assessed (no model analysis); no factual contradictions were "
                "found among the retrieved sources.")
    if contested:
        return ("No interpretive debate backed by at least two sources was found; the disagreement is about "
                "facts (see contested claims).")
    return (f"No meaningful disagreement was found among the {n_sources} retrieved source(s). IntelPod does not "
            f"manufacture a debate where the evidence does not show one.")


def overall_confidence(claims: list[Claim], sources: list[ResearchResult], div: DiversityReport,
                       by_kind: dict[str, list[str]]) -> tuple[str, str]:
    pubs = {s.publisher.lower() for s in sources}
    regions = [r for r in div.regions if r != "unknown"]
    confirmed = len(by_kind["consensus"])
    primary = any(c.primary and c.confidence == "confirmed" for c in claims)
    if len(pubs) < 3 or confirmed == 0:
        return "low", (f"{len(pubs)} publisher(s); {confirmed} independently confirmed statement(s)")
    if confirmed >= 2 and len(pubs) >= 5 and len(regions) >= 2 and primary and \
            len(by_kind["contested"]) <= confirmed // 3:
        return "high", (f"{confirmed} confirmed statements incl. a primary source; {len(pubs)} publishers from "
                        f"{len(regions)} regions")
    return "medium", (f"{confirmed} confirmed statement(s); {len(pubs)} publishers; {len(regions)} region(s); "
                      f"{len(by_kind['contested'])} contested" + ("" if primary else "; no primary source"))
