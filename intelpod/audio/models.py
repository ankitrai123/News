"""Typed contracts for Phase 6 (local audio)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AudioLine(BaseModel):
    line_id: str
    speaker: Literal["A", "B"]
    voice: str
    text: str                                    # what was spoken (after pronunciation/symbol handling)
    file: str | None = None                      # cached per-line WAV (relative to the run's audio dir)
    status: Literal["ok", "failed", "pending"] = "pending"
    attempts: int = 0
    error: str | None = None
    duration_s: float = 0.0
    start_s: float = 0.0                         # position in the final episode
    cached: bool = False


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    severity: Literal["error", "warning"] = "error"


class Chapter(BaseModel):
    title: str
    start_s: float


class AudioRender(BaseModel):
    run_id: str
    episode_id: str
    title: str
    engine: str
    voices: dict[str, str] = Field(default_factory=dict)
    speed: float = 1.0
    sample_rate: int = 0
    status: Literal["complete", "incomplete", "skipped", "failed"]
    script_fp: str = ""                          # which version of the script this audio speaks
    files: dict[str, str] = Field(default_factory=dict)   # format -> path relative to the run directory
    duration_s: float = 0.0
    expected_s: float = 0.0
    lines: list[AudioLine] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime
