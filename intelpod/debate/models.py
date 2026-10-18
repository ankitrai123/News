"""Typed contracts for Phase 3 (viewpoints, claims, agreement and disagreement).

    research package (Phase 2) + source registry
        ─▶ Claim*            verbatim statements from the sources, typed (fact / attributed /
                             interpretation / forecast / value judgement) and grouped across sources
        ─▶ DiversityReport   who is represented: countries, regions, economies, source types,
                             stakeholders, documented editorial orientations, and the gaps
        ─▶ viewpoint searches for missing perspectives (Phase 2 machinery, budgeted)
        ─▶ Question*/Position*  interpretive questions and competing positions (model-assisted,
                             every position tied to retrieved sources and checked)
        ─▶ DebatePackage     agreement, disagreement, unverified claims, uncertainty, confidence

Nothing here invents a source: every source id refers to a ResearchResult that was retrieved
and checked in Phase 2 (or by a Phase 3 viewpoint search, through the same checks).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ClaimKind = Literal["factual", "attributed", "interpretation", "forecast", "value_judgement"]
# Calibrated language (product spec): how well a statement is supported by the retrieved sources.
Confidence = Literal["confirmed", "reported", "plausible", "contested", "speculative"]
DisagreementType = Literal["factual", "causal", "predictive", "value", "none"]
EvidenceStrength = Literal["strong", "moderate", "weak", "none"]

CONFIDENCE_HELP = {
    "confirmed": "stated by a primary source, or by at least two independent sources, and not contradicted",
    "reported": "stated by one source (or copies of one report); not independently confirmed",
    "plausible": "an interpretation consistent with the reported facts, argued by at least one source",
    "contested": "sources contradict each other",
    "speculative": "a forecast or claim about the future or about intentions; cannot be verified yet",
}


class SourceProfile(BaseModel):
    """Registry entry (config/sources.yaml). Unknown is a valid, honest value everywhere."""

    publisher: str
    domains: list[str] = Field(default_factory=list)
    country: str | None = None
    region: str | None = None
    source_type: str | None = None              # news | official | international_org | think_tank | academic | wire
    ownership: str | None = None                # e.g. "public broadcaster", "state-funded", "private"
    editorial_orientation: str = "unknown"      # general orientation, only with references
    economic_orientation: str = "unknown"
    foreign_policy_orientation: str = "unknown"
    strengths: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    corrections_policy_url: str | None = None
    references: list[str] = Field(default_factory=list)   # URLs supporting the factual fields
    orientation_references: list[str] = Field(default_factory=list)  # published studies supporting orientation fields
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    disputed: bool = False
    last_reviewed: str | None = None            # YYYY-MM-DD
    notes: str = ""

    def documented(self, field: str) -> bool:
        """An orientation counts only if it is set AND backed by an orientation-specific reference
        (a general reference such as an ownership page is not evidence of orientation), and is not
        marked disputed."""
        return (getattr(self, field, "unknown") not in ("unknown", "", None) and bool(self.orientation_references)
                and not self.disputed)


class Claim(BaseModel):
    claim_id: str
    text: str                                   # verbatim sentence from a source (never model-written)
    kind: ClaimKind
    kind_evidence: str = ""
    source_ids: list[str] = Field(default_factory=list)       # result_ids stating it (or the same fact)
    independent_sources: int = 0                # publishers, syndicated copies counted once
    primary: bool = False                       # stated by an official/primary source
    contradicted_by: list[str] = Field(default_factory=list)  # result_ids stating the opposite
    confidence: Confidence = "reported"
    confidence_reason: str = ""
    speaker: str | None = None                  # who is quoted, for attributed statements
    stakeholder: str | None = None              # government | corporate | investor | labour | consumer | expert | civil_society | international


class Position(BaseModel):
    label: str                                  # short name of the position
    summary: str                                # model-written, checked against the cited sources
    strongest_argument: str = ""
    source_ids: list[str] = Field(default_factory=list)       # sources arguing it (retrieved, verified ids)
    challenged_by: list[str] = Field(default_factory=list)    # sources with evidence against it
    evidence_strength: EvidenceStrength = "none"
    strength_reason: str = ""
    stakeholders: list[str] = Field(default_factory=list)
    found: bool = True                          # False: the perspective was looked for, not found


class Question(BaseModel):
    question: str
    disagreement_type: DisagreementType = "none"
    positions: list[Position] = Field(default_factory=list)
    asymmetry: str = ""                         # e.g. "most evidence supports A; B rests on one opinion piece"


class DiversityReport(BaseModel):
    sources: int = 0
    countries: dict[str, int] = Field(default_factory=dict)
    regions: dict[str, int] = Field(default_factory=dict)
    economies: dict[str, int] = Field(default_factory=dict)        # advanced / emerging / unknown
    source_types: dict[str, int] = Field(default_factory=dict)
    content_types: dict[str, int] = Field(default_factory=dict)
    stakeholders: dict[str, int] = Field(default_factory=dict)
    orientations: dict[str, int] = Field(default_factory=dict)    # documented only; rest "unknown"
    registry_coverage: str = ""
    gaps: list[str] = Field(default_factory=list)


class ViewpointSearch(BaseModel):
    perspective: str
    reason: str                                  # why IntelPod looked for it
    queries: list[str] = Field(default_factory=list)
    added_source_ids: list[str] = Field(default_factory=list)
    found: bool = False


class DebatePackage(BaseModel):
    run_id: str
    event_id: str
    event_title: str
    status: Literal["completed", "partial", "skipped", "failed"]
    source_ids: list[str] = Field(default_factory=list)          # every source used (Phase 2 + viewpoint searches)
    claims: list[Claim] = Field(default_factory=list)
    consensus: list[str] = Field(default_factory=list)          # claim_ids broadly agreed (confirmed)
    contested: list[str] = Field(default_factory=list)          # claim_ids with contradicting sources
    unverified: list[str] = Field(default_factory=list)         # claim_ids from a single source
    forecasts: list[str] = Field(default_factory=list)          # claim_ids about the future
    questions: list[Question] = Field(default_factory=list)
    no_disagreement_note: str = ""              # "no meaningful disagreement found" when that is the honest result
    uncertainties: list[str] = Field(default_factory=list)
    would_change: list[str] = Field(default_factory=list)       # evidence that would change the assessment
    diversity: DiversityReport = Field(default_factory=DiversityReport)
    viewpoint_searches: list[ViewpointSearch] = Field(default_factory=list)
    overall_confidence: Literal["high", "medium", "low"] = "low"
    confidence_reason: str = ""
    analysed_by: str | None = None              # model id when model analysis was used
    rejected: list[str] = Field(default_factory=list)           # model output IntelPod refused, and why
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime
