"""Typed contracts for Phase 4 (context, business and economic impact, event connections).

    debate package (claims with confidence) + research sources + earlier events in the database
        ─▶ ContextSynthesis   what happened, what is new, timeline, earlier related events
        ─▶ DimensionNote*     which economic dimensions / sectors / India channels the sources discuss
        ─▶ Figure*            numbers stated by sources (measured vs forecast), never computed
        ─▶ CausalChain*       first- and second-order effects; every step cites claims or is
                              labelled as IntelPod's inference (model-proposed, checked)
        ─▶ ImpactAnalysis     per event
    all events of a run ─▶ EventLink*  (evidenced cross-references vs shared drivers)
    forecast claims ─▶ forecast ledger (open until someone records the outcome)
    everything ─▶ IntelligenceBrief

Nothing here estimates a number: figures are quoted from sources with their claim ids.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..debate.models import Confidence

DIMENSIONS: dict[str, str] = {
    "growth": "GDP and economic growth",
    "inflation_rates": "Inflation and interest rates",
    "energy_commodities": "Energy and commodity prices",
    "currency_capital": "Currencies and capital flows",
    "trade_tariffs": "Trade and tariffs",
    "corporate": "Corporate revenues and costs",
    "competition": "Industry structure and competition",
    "supply_chain": "Supply-chain resilience",
    "tech_investment": "Technology investment",
    "jobs_demand": "Employment and consumer demand",
    "fiscal_regulation": "Government budgets and regulation",
}
SECTORS: dict[str, str] = {
    "manufacturing": "Manufacturing", "logistics": "Logistics and shipping", "semiconductors": "Semiconductors",
    "banking": "Banking and finance", "it_services": "IT and software", "energy": "Energy",
    "agriculture": "Agriculture and food", "automobiles": "Automobiles", "pharma": "Pharmaceuticals",
    "metals_mining": "Metals and mining", "defence": "Defence", "consumer": "Consumer goods and retail",
}
Horizon = Literal["short_term", "long_term", "unclear"]
Direction = Literal["positive", "negative", "mixed", "uncertain"]


class DimensionNote(BaseModel):
    """Sources discuss this dimension (or sector): the statements that do, verbatim via claim ids."""

    key: str
    label: str
    claim_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class Figure(BaseModel):
    figure: str                                  # e.g. "5.25%", "$2 billion" as written
    claim_id: str
    text: str                                    # the verbatim claim it comes from
    source_ids: list[str] = Field(default_factory=list)
    kind: Literal["measured", "forecast", "stated"] = "stated"
    confidence: Confidence = "reported"


class CausalStep(BaseModel):
    text: str
    basis: Literal["reported", "inferred"]       # reported = cites claims; inferred = IntelPod/model reasoning
    claim_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = "plausible"


class CausalChain(BaseModel):
    title: str
    dimensions: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    horizon: Horizon = "unclear"
    direction: Direction = "uncertain"
    india: bool = False
    steps: list[CausalStep] = Field(default_factory=list)   # step 1 = first-order (reported), then knock-ons
    status: Literal["measured", "conditional"] = "conditional"
    confidence: Confidence = "speculative"
    confidence_reason: str = ""


class Actor(BaseModel):
    group: str
    effect: Literal["benefits", "harmed", "unintended"]
    reason: str = ""
    claim_ids: list[str] = Field(default_factory=list)
    basis: Literal["reported", "inferred"] = "inferred"


class Interpretation(BaseModel):
    stance: Literal["positive", "negative"]
    summary: str
    claim_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class TimelineEntry(BaseModel):
    when: datetime | None = None
    text: str                                    # verbatim title/claim, or an earlier event's title
    kind: Literal["earlier_event", "background_source", "this_event"]
    ref: str                                     # event_id or result_id
    url: str | None = None
    publisher: str | None = None


class PriorEventRef(BaseModel):
    event_id: str
    title: str
    first_seen_at: datetime | None = None
    last_development_at: datetime | None = None
    shared: list[str] = Field(default_factory=list)   # shared countries/entities/topic
    why: str = ""


class ContextSynthesis(BaseModel):
    what_happened: str = ""
    what_happened_basis: str = ""                 # "lead confirmed claim" / "model summary, checked" / "headline"
    what_happened_claim_ids: list[str] = Field(default_factory=list)
    whats_new: list[str] = Field(default_factory=list)
    previous_run_id: str | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)
    earlier_events: list[PriorEventRef] = Field(default_factory=list)


class ImpactAnalysis(BaseModel):
    run_id: str
    event_id: str
    event_title: str
    status: Literal["completed", "partial", "skipped", "failed"]
    context: ContextSynthesis = Field(default_factory=ContextSynthesis)
    dimensions: list[DimensionNote] = Field(default_factory=list)
    sectors: list[DimensionNote] = Field(default_factory=list)
    india: DimensionNote | None = None
    india_summary: str = ""
    figures: list[Figure] = Field(default_factory=list)
    chains: list[CausalChain] = Field(default_factory=list)
    actors: list[Actor] = Field(default_factory=list)
    interpretations: list[Interpretation] = Field(default_factory=list)
    interpretation_note: str = ""
    not_discussed: list[str] = Field(default_factory=list)  # dimensions no source discusses
    analysed_by: str | None = None
    rejected: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime


class LinkEvidence(BaseModel):
    event_id: str                                # the event whose source mentions the other
    result_id: str
    publisher: str
    url: str
    text: str                                    # verbatim title/extract/claim


class EventLink(BaseModel):
    run_id: str
    link_id: str
    event_a: str
    event_b: str
    title_a: str
    title_b: str
    kind: Literal["evidenced", "shared_driver"]
    channels: list[str] = Field(default_factory=list)    # e.g. energy prices, interest rates
    shared: list[str] = Field(default_factory=list)      # shared countries / entities
    evidence: list[LinkEvidence] = Field(default_factory=list)
    explanation: str = ""
    explanation_by: str | None = None
    note: str = ""


class Forecast(BaseModel):
    """Forecast ledger entry: an analyst's or official's statement about the future, never a fact."""

    forecast_id: str
    text: str                                    # verbatim
    event_id: str
    event_title: str = ""
    first_run_id: str
    last_run_id: str
    first_seen_at: datetime
    publisher: str | None = None
    url: str | None = None
    speaker: str | None = None
    published_at: datetime | None = None
    timeframe: str | None = None                 # as stated ("next year", "by 2027"); None if not stated
    hedge: str | None = None                     # the source's own wording ("expected", "could")
    source_ids: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved"] = "open"
    verdict: Literal["correct", "incorrect", "partly", "unresolvable"] | None = None
    outcome: str = ""
    evaluated_at: datetime | None = None
    history: list[dict] = Field(default_factory=list)   # earlier verdicts, when an outcome was amended


class IntelligenceBrief(BaseModel):
    run_id: str
    generated_for: datetime
    event_ids: list[str] = Field(default_factory=list)
    links: list[EventLink] = Field(default_factory=list)
    new_forecasts: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime
