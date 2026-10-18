"""Who is represented among an event's sources, and what is missing.

Diversity is judged issue by issue from facts: where sources are from (country, region,
advanced vs emerging economy), what kind of source they are, who they quote (government,
companies, investors, labour, consumers, experts, civil society, international bodies) and,
only where the registry documents it with references, editorial orientation. Gaps become
targeted viewpoint searches; perspectives still missing afterwards are reported, never invented.
"""
from __future__ import annotations

from collections import Counter

from ..research.models import EventBrief, ResearchResult
from .models import Claim, DiversityReport
from .registry import REGION_BY_CODE, Registry, country_code, economy

ECON_TOPICS = {"macroeconomy", "markets_monetary", "trade_policy", "energy_commodities", "corporate",
               "semiconductors_minerals", "supply_chain", "technology_ai"}


def source_country(r: ResearchResult, registry: Registry) -> str | None:
    prof = registry.lookup(r.domain, r.publisher)
    if prof and prof.country:
        return prof.country
    return country_code(getattr(r, "source_country", None))


def diversity_report(brief: EventBrief, sources: list[ResearchResult], claims: list[Claim],
                     registry: Registry) -> DiversityReport:
    countries, regions, economies, types, contents, orient = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    in_registry = 0
    for r in sources:
        cc = source_country(r, registry)
        countries[cc or "unknown"] += 1
        regions[REGION_BY_CODE.get(cc or "", "unknown")] += 1
        economies[economy(cc)] += 1
        types[r.source_type or "unknown"] += 1
        contents[r.content_type] += 1
        prof = registry.lookup(r.domain, r.publisher)
        in_registry += prof is not None
        label = prof.editorial_orientation if prof and prof.documented("editorial_orientation") else "unknown"
        orient[label] += 1
    stakes = Counter(c.stakeholder for c in claims if c.stakeholder)
    rep = DiversityReport(sources=len(sources), countries=dict(countries), regions=dict(regions),
                          economies=dict(economies), source_types=dict(types), content_types=dict(contents),
                          stakeholders=dict(stakes), orientations=dict(orient),
                          registry_coverage=f"{in_registry} of {len(sources)} sources are in the source registry")
    rep.gaps = gaps(brief, rep, sources, registry)
    return rep


def gaps(brief: EventBrief, rep: DiversityReport, sources: list[ResearchResult], registry: Registry) -> list[str]:
    out: list[str] = []
    if not sources:
        return ["No sources to compare."]
    known_regions = [r for r in rep.regions if r != "unknown"]
    if len(known_regions) == 1 and len(sources) >= 3:
        out.append(f"All located sources are from one region ({known_regions[0]}).")
    party_codes = [c for c in (country_code(x) for x in brief.countries) if c]
    present = {k for k in rep.countries if k != "unknown"}
    for cc in party_codes:
        if cc not in present and cc != "EU":
            out.append(f"No source based in {cc}, a party to this event.")
    if any(economy(c) == "emerging" for c in party_codes) and not rep.economies.get("emerging"):
        out.append("No source from an emerging economy, although emerging economies are involved.")
    if not ({"analysis", "opinion", "research"} & set(rep.content_types)):
        out.append("No analysis, opinion or research: only straight reporting.")
    if not ({"primary"} & set(rep.content_types)):
        out.append("No primary source (official statement or document).")
    have = set(rep.stakeholders)
    wanted = ["government", "expert"] + (["corporate", "investor"] if brief.topic in ECON_TOPICS else [])
    missing = [w for w in wanted if w not in have]
    if missing:
        out.append("No quoted voices from: " + ", ".join(m.replace("_", " ") for m in missing) + ".")
    if set(rep.orientations) == {"unknown"}:
        out.append("Editorial orientation is not documented for any of these sources (see config/sources.yaml); "
                   "diversity of positions is judged from what the sources argue, not from labels.")
    return out
