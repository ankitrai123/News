"""Publication-date validation for research results.

A result's publication date is NOT the date of the event it discusses. IntelPod records both:
`published_at` (best available evidence of when the source was published) and the event's own
date, then classifies the source as before / during / after the event.

Precedence: the page's own metadata (original publication, never "modified") > feed date >
API date > GDELT's "first seen" time (an approximation, flagged) > none.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import PageInfo

RANK = {"page_metadata": 0, "feed": 1, "api": 2, "gdelt_seendate": 3, "missing": 4}
FUTURE_TOLERANCE = timedelta(hours=2)
EARLIEST = datetime(1995, 1, 1, tzinfo=timezone.utc)
CONFLICT = timedelta(days=3)
DURING = timedelta(hours=24)


@dataclass
class DateResolution:
    published_at: datetime | None
    date_source: str
    notes: list[str] = field(default_factory=list)
    in_window: bool = True
    event_timing: str = "undated"
    keep: bool = True


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def resolve_date(candidates: list[tuple[datetime | None, str]], page: PageInfo | None, now: datetime,
                 window_start: datetime, background_start: datetime, event_date: datetime | None) -> DateResolution:
    notes: list[str] = []
    found: list[tuple[datetime, str]] = []
    if page is not None and page.published_at is not None:
        found.append((_aware(page.published_at), "page_metadata"))
    for value, source in candidates:
        if value is not None and source != "missing":
            found.append((_aware(value), source))
    valid = []
    for d, src in found:
        if d > now + FUTURE_TOLERANCE:
            notes.append(f"ignored a {src.replace('_', ' ')} date in the future ({d:%Y-%m-%d})")
        elif d < EARLIEST:
            notes.append(f"ignored an implausible {src.replace('_', ' ')} date ({d:%Y-%m-%d})")
        else:
            valid.append((d, src))
    if not valid:
        return DateResolution(None, "missing", notes + ["no publication date found"], in_window=True,
                              event_timing="undated", keep=True)
    valid.sort(key=lambda x: (RANK.get(x[1], 9), x[0]))
    best, src = valid[0]
    others = [(d, s) for d, s in valid[1:] if s != src]
    if others and abs(others[0][0] - best) > CONFLICT:
        notes.append(f"dates disagree: {src.replace('_', ' ')} says {best:%Y-%m-%d}, "
                     f"{others[0][1].replace('_', ' ')} says {others[0][0]:%Y-%m-%d}; using the first")
    if src == "gdelt_seendate":
        notes.append("approximate: the time GDELT first saw the page, not a publisher date")
    in_window = window_start <= best <= now + FUTURE_TOLERANCE
    keep = best >= background_start
    if not in_window and keep:
        notes.append("older than the research window: kept only as background")
    timing = "undated"
    if event_date is not None:
        ev = _aware(event_date)
        timing = "before_event" if best < ev - DURING else ("during_event" if best <= ev + DURING else "after_event")
    return DateResolution(best, src, notes, in_window, timing, keep)
