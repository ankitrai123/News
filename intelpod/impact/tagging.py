"""Deterministic impact evidence: which dimensions, sectors and India channels the sources
discuss, and which figures they state. Works on the verbatim claim map (Phase 3), so every
entry points at statements a reader can check. Nothing here predicts anything."""
from __future__ import annotations

import re

from ..debate.claims import NUMBER
from ..debate.models import Claim
from .models import DIMENSIONS, SECTORS, DimensionNote, Figure


def _rx(words: str) -> re.Pattern:
    return re.compile(r"\b(" + words + r")\b", re.I)


DIMENSION_RX: dict[str, re.Pattern] = {
    # Every growth cue names the economy: "network slowdown" or "sales growth" are not GDP.
    "growth": _rx(r"gdp|economic growth|growth forecasts?|growth outlook|recession|economic slowdown|"
                  r"slowdown in (?:the )?(?:global |world |\w+n )?economy|economic output|economic activity|"
                  r"econom(?:y|ies) (?:grew|shrank|contracted|expanded|slowed|slowing|grow|growing)|"
                  r"(?:global|world) economy|economic contraction"),
    "inflation_rates": _rx(r"inflation|interest rates?|repo rate|policy rate|rate cuts?|rate hikes?|central bank|"
                           r"monetary policy|bond yields?|yields|cpi|consumer prices"),
    "energy_commodities": _rx(r"oil|crude|brent|natural gas|lng|opec\+?|fuel|petrol|diesel|coal|copper|gold|"
                              r"commodit(?:y|ies)|wheat|metals?|lithium|rare earths?"),
    "currency_capital": _rx(r"rupee|dollar|yuan|yen|euro|currency|currencies|exchange rate|capital flows?|"
                            r"foreign investors?|fdi|portfolio flows?|outflows?|inflows?|forex|reserves"),
    "trade_tariffs": _rx(r"tariffs?|duties|duty|trade deal|trade war|exports?|imports?|export controls?|"
                         r"trade deficit|trade surplus|quotas?|sanctions?|wto"),
    # Company finances only: "energy revenues" or "tax revenues" of a state are fiscal, not corporate.
    "corporate": re.compile(r"\b((?:company|companies|firm|firms|corporate|group|maker|makers|retailer|retailers|"
                            r"bank|banks|lender|lenders|insurer|insurers|automaker|automakers|carmaker|carmakers|"
                            r"airline|airlines|refiner|refiners|exporters?|manufacturers?|business(?:es)?|"
                            r"inc\.?|ltd\.?|plc)(?:'s|’s)?\W+(?:\w+\W+){0,3}(?:revenues?|profits?|earnings|"
                            r"margins?|costs?|sales|orders|losses|guidance)|quarterly (?:results|earnings|profit|"
                            r"revenue)|net profit|operating profit|profit warning|earnings per share|"
                            r"(?:shares|stock) (?:rose|fell|jumped|slumped|surged|tumbled|dropped|climbed|gained|"
                            r"plunged|rallied)|market (?:value|capitali[sz]ation)|input costs|operating costs)\b",
                            re.I),
    "competition": _rx(r"competition|competitors?|market share|antitrust|monopoly|consolidation|mergers?|"
                       r"acquisitions?|rivals?"),
    "supply_chain": _rx(r"supply chains?|suppliers?|shortages?|logistics|shipping|freight|bottlenecks?|"
                        r"inventor(?:y|ies)|sourcing|reshoring|friend-shoring|disruptions?"),
    "tech_investment": _rx(r"investment in|capital expenditure|capex|data centres?|data centers?|"
                           r"artificial intelligence|\bai\b|chipmaking|fabs?|r&d|research and development"),
    "jobs_demand": _rx(r"jobs?|employment|unemployment|layoffs?|hiring|workers|wages|consumer demand|"
                       r"consumer spending|households|retail sales"),
    "fiscal_regulation": _rx(r"budget|deficit|fiscal|subsid(?:y|ies)|tax(?:es)?|tax revenues?|"
                             r"(?:oil|energy|gas|state|government|export) revenues?|regulat\w*|rules|law|"
                             r"legislation|ban|licen[cs]e|compliance"),
}
SECTOR_RX: dict[str, re.Pattern] = {
    "manufacturing": _rx(r"manufactur\w*|factor(?:y|ies)|industrial output|production lines?|assembly"),
    "logistics": _rx(r"logistics|shipping|freight|ports?|container|airlines?|air cargo"),
    "semiconductors": _rx(r"semiconductors?|chips?|chipmak\w*|foundr(?:y|ies)|wafers?|fabs?"),
    "banking": _rx(r"banks?|banking|lenders?|lending|loans?|credit|nbfcs?|insurers?|financial sector"),
    "it_services": _rx(r"it services|software|outsourcing|tech companies|technology companies|cloud|"
                       r"infosys|tcs|wipro"),
    "energy": _rx(r"oil|gas|refiner\w*|power|electricity|renewables?|solar|coal|energy"),
    "agriculture": _rx(r"agricultur\w*|farm\w*|crops?|wheat|rice|fertili[sz]ers?|food prices"),
    "automobiles": _rx(r"automakers?|carmakers?|cars?|vehicles?|\bevs?\b|auto industry|automobiles?"),
    "pharma": _rx(r"pharma\w*|drugs?|medicines?|generics?"),
    "metals_mining": _rx(r"steel|aluminium|aluminum|copper|mining|miners?|minerals?|rare earths?|lithium"),
    "defence": _rx(r"defen[cs]e|military|weapons?|arms"),
    "consumer": _rx(r"retail|consumers?|consumer goods|fmcg|shoppers"),
}
INDIA_RX = _rx(r"india|indian|new delhi|delhi|mumbai|rbi|reserve bank of india|rupee|sensex|nifty|sebi|"
               r"modi|niti aayog|bengaluru|chennai|kolkata")
TIMEFRAME = re.compile(r"\b(next (?:week|month|quarter|year|decade)|this (?:week|month|quarter|year)|"
                       r"(?:in|by|before|until) (?:early |mid-|late |end of )?(?:19|20)\d\d|"
                       r"(?:in|by) the (?:coming|next) \w+(?: \w+)?|within \w+ (?:days|weeks|months|years)|"
                       r"(?:fiscal|financial) year \S+|fy\s?\d{2,4}|q[1-4](?: (?:19|20)\d\d)?|"
                       r"(?:in|by) (?:january|february|march|april|may|june|july|august|september|october|"
                       r"november|december)(?: (?:19|20)\d\d)?|short[- ]term|long[- ]term|medium[- ]term)\b", re.I)


def _note(key: str, label: str, claims: list[Claim]) -> DimensionNote:
    return DimensionNote(key=key, label=label, claim_ids=[c.claim_id for c in claims],
                         source_ids=list(dict.fromkeys(s for c in claims for s in c.source_ids)))


def tag_dimensions(claims: list[Claim]) -> tuple[list[DimensionNote], list[DimensionNote], DimensionNote | None]:
    """(dimensions, sectors, india) discussed by the claims, most-discussed first."""
    dims = [_note(k, DIMENSIONS[k], [c for c in claims if rx.search(c.text)]) for k, rx in DIMENSION_RX.items()]
    secs = [_note(k, SECTORS[k], [c for c in claims if rx.search(c.text)]) for k, rx in SECTOR_RX.items()]
    india = [c for c in claims if INDIA_RX.search(c.text)]
    dims = sorted((d for d in dims if d.claim_ids), key=lambda d: -len(d.source_ids))
    secs = sorted((d for d in secs if d.claim_ids), key=lambda d: -len(d.source_ids))
    return dims, secs, (_note("india", "India", india) if india else None)


_YEAR = re.compile(r"^(19|20)\d\d$")


def figures(claims: list[Claim]) -> list[Figure]:
    """Numbers with a unit (%, bps, billion...) or currency, quoted from claims. Years and bare
    counts are skipped. A forecast claim's figure is a forecast, never a measurement."""
    out: list[Figure] = []
    seen: set[tuple[str, str]] = set()
    for c in claims:
        for m in NUMBER.finditer(c.text):
            num, unit = m.group(1), (m.group(2) or "")
            before = c.text[max(0, m.start() - 1):m.start()]
            currency = before in ("$", "€", "£", "¥", "₹")
            if not unit and not currency:
                continue
            if _YEAR.match(num.replace(",", "")) and not unit:
                continue
            fig = f"{before if currency else ''}{num}{'' if not unit else ('%' if unit == '%' else ' ' + unit)}"
            key = (fig.lower(), c.claim_id)
            if key in seen:
                continue
            seen.add(key)
            # "measured" only for an uncontested plain fact; an attributed figure is what someone stated.
            kind = "forecast" if c.kind == "forecast" else (
                "measured" if c.kind == "factual" and c.confidence in ("confirmed", "reported") else "stated")
            out.append(Figure(figure=fig, claim_id=c.claim_id, text=c.text, source_ids=c.source_ids, kind=kind,
                              confidence=c.confidence))
    return out[:20]


def timeframe(text: str) -> str | None:
    m = TIMEFRAME.search(text)
    return m.group(1) if m else None
