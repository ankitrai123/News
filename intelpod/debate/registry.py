"""Source registry: load config/sources.yaml, look publishers up by domain, classify economies.

Orientation fields count only when documented (set AND referenced); everything else is
reported as "unknown". The economy classification uses the IMF World Economic Outlook grouping
(advanced economies vs emerging market and developing economies), a published, factual list.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .models import SourceProfile

log = logging.getLogger(__name__)

# IMF World Economic Outlook "advanced economies" grouping (as published up to 2025; check the
# WEO "Groups and Aggregates" page, https://www.imf.org/en/Publications/WEO), by ISO alpha-2 code.
# "EU" (the European Union as an actor) is treated as advanced.
IMF_ADVANCED = frozenset(
    "AD AT AU BE CA CH CY CZ DE DK EE ES FI FR GB GR HK HR IE IL IS IT JP KR LT LU LV MO MT NL NO NZ PR PT SE SG "
    "SI SK SM TW US EU".split())

NAME_TO_CODE = {
    "united states": "US", "united kingdom": "GB", "britain": "GB", "india": "IN", "china": "CN", "russia": "RU",
    "japan": "JP", "germany": "DE", "france": "FR", "italy": "IT", "canada": "CA", "australia": "AU",
    "south korea": "KR", "korea": "KR", "singapore": "SG", "hong kong": "HK", "taiwan": "TW", "brazil": "BR",
    "mexico": "MX", "indonesia": "ID", "turkey": "TR", "saudi arabia": "SA", "united arab emirates": "AE",
    "qatar": "QA", "israel": "IL", "iran": "IR", "pakistan": "PK", "bangladesh": "BD", "south africa": "ZA",
    "nigeria": "NG", "kenya": "KE", "egypt": "EG", "ukraine": "UA", "poland": "PL", "spain": "ES",
    "netherlands": "NL", "switzerland": "CH", "sweden": "SE", "norway": "NO", "ireland": "IE", "belgium": "BE",
    "philippines": "PH", "vietnam": "VN", "thailand": "TH", "malaysia": "MY", "argentina": "AR", "new zealand": "NZ",
    "european union": "EU", "sri lanka": "LK", "nepal": "NP",
}
REGION_BY_CODE = {
    **{c: "Europe" for c in "GB DE FR IT ES NL BE CH SE NO IE PL PT AT DK FI GR CZ UA EU".split()},
    **{c: "North America" for c in "US CA MX".split()},
    **{c: "South Asia" for c in "IN PK BD LK NP".split()},
    **{c: "Asia" for c in "CN JP KR SG HK TW ID PH VN TH MY".split()},
    **{c: "Middle East" for c in "QA SA AE IL IR TR EG".split()},
    **{c: "Africa" for c in "ZA NG KE".split()},
    **{c: "Latin America" for c in "BR AR".split()},
    **{c: "Oceania" for c in "AU NZ".split()},
    "RU": "Eurasia",
}


def country_code(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return NAME_TO_CODE.get(v.lower())


def economy(code: str | None) -> str:
    if not code:
        return "unknown"
    return "advanced" if code in IMF_ADVANCED else "emerging"


class Registry:
    def __init__(self, profiles: list[SourceProfile]):
        self.profiles = profiles
        self._by_domain: dict[str, SourceProfile] = {}
        self._by_name: dict[str, SourceProfile] = {}
        for p in profiles:
            for d in p.domains:
                self._by_domain[d.lower().removeprefix("www.")] = p
            self._by_name[p.publisher.lower()] = p

    @classmethod
    def load(cls, path: Path) -> "Registry":
        if not path.exists():
            return cls([])
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("%s could not be read (%s); continuing without the source registry", path, e)
            return cls([])
        if not isinstance(data, dict) or not isinstance(data.get("sources") or [], list):
            log.warning("%s: expected a mapping with a 'sources' list; continuing without the source registry", path)
            return cls([])
        out = []
        for i, row in enumerate(data.get("sources") or []):
            try:
                out.append(SourceProfile.model_validate(row))
            except Exception as e:  # noqa: BLE001 - one bad entry must not disable the registry
                log.warning("sources.yaml entry %d ignored: %s", i, e)
        return cls(out)

    def lookup(self, domain: str | None = None, publisher: str | None = None) -> SourceProfile | None:
        d = (domain or "").lower().removeprefix("www.")
        while d:
            if d in self._by_domain:
                return self._by_domain[d]
            d = d.split(".", 1)[1] if "." in d else ""
        return self._by_name.get((publisher or "").lower()) if publisher else None

    def editorial_metadata(self, domain: str | None, publisher: str | None) -> dict:
        """What research results show as `editorial_metadata` (documented facts only)."""
        p = self.lookup(domain, publisher)
        if p is None:
            return {"status": "not in registry", "orientation": "unknown"}
        meta = {"status": "registry", "publisher": p.publisher, "country": p.country, "region": p.region,
                "source_type": p.source_type, "ownership": p.ownership, "references": p.references,
                "confidence": p.confidence, "last_reviewed": p.last_reviewed}
        for f in ("editorial_orientation", "economic_orientation", "foreign_policy_orientation"):
            meta[f] = getattr(p, f) if p.documented(f) else "unknown"
        if p.disputed:
            meta["disputed"] = True
        if p.limitations:
            meta["limitations"] = p.limitations
        if p.corrections_policy_url:
            meta["corrections_policy_url"] = p.corrections_policy_url
        return meta
