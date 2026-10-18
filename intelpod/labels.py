"""Human-readable labels and small formatting helpers for the dashboard and reports."""
from __future__ import annotations

import re
from datetime import datetime

from .textutil import country_alias_tokens

TOPIC_LABELS = {
    "geopolitics": "Geopolitics", "elections_politics": "Elections & politics", "macroeconomy": "Macroeconomy",
    "trade_policy": "Trade policy", "energy_commodities": "Energy & commodities",
    "markets_monetary": "Markets & monetary policy", "corporate": "Companies", "technology_ai": "Technology & AI",
    "semiconductors_minerals": "Chips & critical minerals", "supply_chain": "Supply chains", "defense": "Defence",
    "other": "Other",
}

FLAG_LABELS = {
    "some_dates_approximate": "approximate dates",
    "dates_approximate": "approximate dates",
    "single_source": "single source",
    "soft_news": "soft news",
    "off_taxonomy": "unclassified topic",
    "quiet_day_fill": "below usual threshold",
    "model_removed_outliers": "off-topic reports removed",
    "model_text_rejected": "model text rejected",
    "not_model_reviewed": "not model-reviewed",
}

FLAG_HELP = {
    "dates_approximate": "None of these reports has a publisher timestamp; times are when GDELT first saw them.",
    "some_dates_approximate": "Some reports only have the time GDELT first saw them, not a publication time.",
    "single_source": "Only one independent publisher has reported this so far.",
    "quiet_day_fill": "Included because fewer events than usual met the normal threshold.",
    "model_text_rejected": "The model's title or summary named facts not found in the reports, so it was discarded.",
    "not_model_reviewed": "Promoted into the briefing after the model-review budget was used.",
}

DATE_SOURCE_LABELS = {
    "feed": "", "api": "", "gdelt_seendate": "time first seen by GDELT", "missing": "no date given",
    "invalid": "invalid date in feed",
}


def topic_label(topic: str | None, subtopic: str | None = None) -> str:
    base = TOPIC_LABELS.get(topic or "other", (topic or "other").replace("_", " ").capitalize())
    return f"{base} · {subtopic}" if subtopic else base


def flag_label(flag: str) -> str:
    return FLAG_LABELS.get(flag, flag.replace("_", " "))


def flag_help(flag: str) -> str:
    return FLAG_HELP.get(flag, "")


def date_source_label(src: str | None) -> str:
    return DATE_SOURCE_LABELS.get(src or "", (src or "").replace("_", " "))


def plural(n: int, word: str, plural_form: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural_form or word + 's')}"


def dt_utc(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M UTC") if d else "—"


def display_entities(entities: list[str] | None, countries: list[str] | None = None, limit: int = 8) -> list[str]:
    """Drop entities that just repeat a country (Russia/Russian/US), de-duplicate, title-case."""
    aliases = country_alias_tokens()
    country_words = {w.lower() for c in (countries or []) for w in c.split()}
    out: dict[str, str] = {}
    for e in entities or []:
        words = [w for w in re.split(r"[\s.\-]+", e.lower()) if w]
        if not words or all(w in aliases or w in country_words for w in words):
            continue
        key = e.lower().rstrip("s")
        if key not in out:
            out[key] = e if any(ch.isupper() for ch in e) else " ".join(w.capitalize() for w in e.split())
    return list(out.values())[:limit]
