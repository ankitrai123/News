"""Remember GDELT refusals between runs.

Within a run, GDELT requests are spaced and a refusal triggers a cool-down. Without memory
between runs, every new run (and `intelpod doctor`) asks again while GDELT is still refusing,
which can keep its block going. After a run in which GDELT refused and nothing succeeded,
IntelPod skips GDELT for 30 minutes, doubling after each further refused run up to 6 hours.
One successful request resets it.

Stored in the `meta` table: `gdelt_backoff` = {"level": n, "until": iso-time}.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ..db import Database, MetaRow

log = logging.getLogger(__name__)
KEY = "gdelt_backoff"
BASE = timedelta(minutes=30)
MAX = timedelta(hours=6)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _load(db: Database) -> dict:
    with db.session() as s:
        row = s.get(MetaRow, KEY)
        if row is None:
            return {}
        try:
            return json.loads(row.value) or {}
        except ValueError:
            return {}


def blocked_until(db: Database, now: datetime | None = None) -> datetime | None:
    """When GDELT may be asked again, or None if it may be asked now."""
    st = _load(db)
    try:
        until = datetime.fromisoformat(st["until"]) if st.get("until") else None
    except ValueError:
        return None
    return until if until and until > _now(now) else None


def note(db: Database, records, now: datetime | None = None) -> None:
    """Update the back-off from one run's GDELT fetch records (FetchRecord-like objects)."""
    records = list(records)
    if not records:
        return
    ok = any(getattr(r, "ok", False) for r in records)
    refused = any(getattr(r, "status_code", None) == 429 or "limit requests" in (getattr(r, "error", "") or "").lower()
                  or "rate limit" in (getattr(r, "error", "") or "").lower() for r in records)
    st = _load(db)
    if ok and not refused:
        st = {"level": 0, "until": None}
    elif ok:
        return  # mixed: GDELT answered some requests; keep the current level, no new block
    elif refused:
        level = int(st.get("level") or 0) + 1
        wait = min(BASE * (2 ** (level - 1)), MAX)
        st = {"level": level, "until": (_now(now) + wait).isoformat()}
        log.warning("GDELT refused every request this run; it will be skipped for %d min", wait.total_seconds() // 60)
    else:
        return
    with db.session() as s:
        s.merge(MetaRow(key=KEY, value=json.dumps(st)))


def reason(until: datetime) -> str:
    return (f"GDELT skipped until {until:%H:%M} UTC: it refused requests in a recent run (waiting avoids "
            f"extending its block; shared networks such as campus Wi-Fi are often rate-limited)")
