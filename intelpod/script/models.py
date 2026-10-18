"""Typed contracts for Phase 5 (podcast script).

    intelligence brief (Phase 4) + debate packages (Phase 3) + research sources (Phase 2)
        ─▶ EpisodePlan    story selection and order, segments, narrative connections (deterministic)
        ─▶ FactSheet      per story: numbered items (verbatim claims, positions, effect chains,
                          India statements, forecasts, uncertainties) with sources and confidence
        ─▶ ScriptLine*    two-host dialogue; every factual line cites fact-sheet items
        ─▶ verification   every line checked against what it cites; fixed, revised once, or removed
        ─▶ Episode        validated script + source-linked transcript + episode notes + evidence report

A line never cites a source directly: it cites fact-sheet items, which point at claims, which
point at retrieved sources. So every sentence is traceable to retrieved evidence or is labelled
as analysis/explanation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SegmentKind = Literal["opening", "story", "watch", "closing"]
LineKind = Literal["fact", "analysis", "question", "transition", "uncertainty", "explainer"]
CheckStatus = Literal["supported", "fixed", "revised", "removed", "unchecked"]


class FactItem(BaseModel):
    fid: str                                     # e.g. "F3" (unique within the episode)
    kind: Literal["fact", "contested", "forecast", "position", "chain", "india", "uncertainty", "link", "figure"]
    text: str                                    # verbatim claim, or a checked Phase 3/4 item
    confidence: str = "reported"
    event_id: str
    claim_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    publishers: list[str] = Field(default_factory=list)
    basis: str = "sourced"                       # sourced | inference | quoted statements


class StoryPlan(BaseModel):
    event_id: str
    title: str
    rank: int | None = None
    why: str = ""                                # why selected and placed here
    word_budget: int = 400
    facts: list[str] = Field(default_factory=list)       # fact ids for this story
    connects_to: list[str] = Field(default_factory=list) # event_ids of linked stories (evidenced links)
    india: bool = False


class EpisodePlan(BaseModel):
    stories: list[StoryPlan] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)      # events not used, with reasons
    target_words: int = 1800


class LineCheck(BaseModel):
    status: CheckStatus = "unchecked"
    problems: list[str] = Field(default_factory=list)
    original: str | None = None                  # text before an automatic fix or model revision


class ScriptLine(BaseModel):
    line_id: str
    segment: SegmentKind
    event_id: str | None = None
    speaker: Literal["A", "B"]
    text: str
    kind: LineKind = "fact"
    cites: list[str] = Field(default_factory=list)       # fact ids
    check: LineCheck = Field(default_factory=LineCheck)


class Pronunciation(BaseModel):
    name: str
    say: str                                     # respelling, e.g. "ZEL-en-skee"; a suggestion for TTS


class GlossaryItem(BaseModel):
    term: str
    explanation: str                             # general knowledge, not from the sources (labelled)


class VerificationSummary(BaseModel):
    lines: int = 0
    supported: int = 0
    fixed: int = 0
    revised: int = 0
    removed: int = 0
    problems: list[str] = Field(default_factory=list)    # what was removed, and why


class Episode(BaseModel):
    run_id: str
    episode_id: str
    title: str
    hosts: dict[str, str] = Field(default_factory=dict)  # {"A": "Maya", "B": "Arjun"}
    status: Literal["validated", "partial", "skipped", "failed"]
    written_by: str = "template"                 # model id, or "template" (no model)
    plan: EpisodePlan = Field(default_factory=EpisodePlan)
    facts: list[FactItem] = Field(default_factory=list)
    lines: list[ScriptLine] = Field(default_factory=list)  # final script (removed lines excluded)
    removed: list[ScriptLine] = Field(default_factory=list)
    pronunciations: list[Pronunciation] = Field(default_factory=list)
    glossary: list[GlossaryItem] = Field(default_factory=list)
    word_count: int = 0
    est_minutes: float = 0.0
    verification: VerificationSummary = Field(default_factory=VerificationSummary)
    limitations: list[str] = Field(default_factory=list)
    inputs: str = ""                             # fingerprint of the Phase 2-4 material and writing mode
    created_at: datetime
