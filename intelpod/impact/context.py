"""Context synthesis (deterministic): what happened, what is new since IntelPod last looked,
a dated timeline, and earlier events in IntelPod's own database that share this event's actors.

Earlier events are listed as *related by shared countries/entities*, never as causes: a causal
claim needs a source saying so (see links.py)."""
from __future__ import annotations

from datetime import datetime, timedelta

from ..debate.models import Claim, DebatePackage
from ..research.models import EventBrief, ResearchResult
from .models import ContextSynthesis, ImpactAnalysis, PriorEventRef, TimelineEntry

LOOKBACK_DAYS = 45  # default; settings.impact.context_lookback_days


def lead_claim(claims: list[Claim]) -> Claim | None:
    """The best-supported plain factual statement: confirmed first, most sources, primary."""
    facts = [c for c in claims if c.kind in ("factual", "attributed") and c.confidence in ("confirmed", "reported")]
    if not facts:
        return None
    return sorted(facts, key=lambda c: (c.confidence != "confirmed", not c.primary, -c.independent_sources,
                                        len(c.text)))[0]


def whats_new(claims: list[Claim], previous: ImpactAnalysis | None, prev_claims: list[Claim] | None,
              first_seen: datetime | None) -> list[str]:
    if previous is None:
        return ["First time IntelPod analyses this event" + (f" (first seen {first_seen:%Y-%m-%d})." if first_seen
                                                             else ".")]
    old = {" ".join(c.text.lower().split()) for c in (prev_claims or [])}
    fresh = [c for c in claims if " ".join(c.text.lower().split()) not in old
             and c.kind in ("factual", "attributed", "forecast")]
    fresh.sort(key=lambda c: (c.confidence != "confirmed", -c.independent_sources))
    out = [f"Since run {previous.run_id} ({previous.created_at:%Y-%m-%d}): {len(fresh)} new statement(s) in the "
           f"sources."]
    out += [f"New: “{c.text}” ({c.confidence})" for c in fresh[:4]]
    if not fresh:
        out = [f"No new statements since run {previous.run_id} ({previous.created_at:%Y-%m-%d}); the story may "
               f"be continuing without new facts."]
    return out


def earlier_events(brief: EventBrief, rows, limit: int = 6) -> list[PriorEventRef]:
    countries = {c.lower() for c in brief.countries}
    entities = {e.lower() for e in brief.entities}
    out = []
    for r in rows:
        shared_c = sorted(countries & {c.lower() for c in (r.countries or [])})
        shared_e = sorted(entities & {e.lower() for e in (r.entities or [])})
        same_topic = r.topic == brief.topic
        # Shared named entities, or the same topic in the same countries: worth showing as context.
        if not shared_e and not (same_topic and shared_c):
            continue
        shared = [f"entity: {e}" for e in shared_e] + [f"country: {c}" for c in shared_c] + \
            (["same topic"] if same_topic else [])
        out.append((len(shared_e) * 2 + len(shared_c) + same_topic, PriorEventRef(
            event_id=r.event_id, title=r.title, first_seen_at=r.first_seen_at,
            last_development_at=r.last_development_at, shared=shared,
            why="shares " + ", ".join(shared) + " (context, not a demonstrated cause)")))
    out.sort(key=lambda x: (-x[0], -(x[1].first_seen_at.timestamp() if x[1].first_seen_at else 0)))
    return [p for _, p in out[:limit]]


def timeline(brief: EventBrief, sources: list[ResearchResult], prior: list[PriorEventRef],
             lead: Claim | None) -> list[TimelineEntry]:
    items: list[TimelineEntry] = []
    for p in prior:
        items.append(TimelineEntry(when=p.first_seen_at, text=p.title, kind="earlier_event", ref=p.event_id))
    for r in sources:
        if r.event_timing == "before_event" and r.published_at:
            items.append(TimelineEntry(when=r.published_at, text=r.title, kind="background_source", ref=r.result_id,
                                       url=r.url, publisher=r.publisher))
    # The event's own row pairs its first report's time with the event title, never with a
    # statement from a different (later) article.
    items.append(TimelineEntry(when=brief.first_article_at, text=f"First report: {brief.headline or brief.title}",
                               kind="this_event", ref=brief.event_id))
    items.sort(key=lambda t: (t.when is None, t.when or datetime.min))
    return items[-12:]


def build_context(brief: EventBrief, debate: DebatePackage | None, sources: list[ResearchResult], store,
                  previous: ImpactAnalysis | None, prev_claims: list[Claim] | None, now: datetime,
                  lookback_days: int = LOOKBACK_DAYS) -> ContextSynthesis:
    claims = debate.claims if debate else []
    lead = lead_claim(claims)
    start = brief.first_article_at or now
    rows = store.earlier_events(brief.event_id, before=start, since=start - timedelta(days=lookback_days))
    prior = earlier_events(brief, rows)
    ctx = ContextSynthesis(
        what_happened=lead.text if lead else (brief.headline or brief.title),
        what_happened_basis=(f"lead {lead.confidence} statement from the sources" if lead else "publisher headline"),
        what_happened_claim_ids=[lead.claim_id] if lead else [],
        whats_new=whats_new(claims, previous, prev_claims, brief.first_article_at),
        previous_run_id=previous.run_id if previous else None,
        earlier_events=prior, timeline=timeline(brief, sources, prior, lead))
    return ctx
