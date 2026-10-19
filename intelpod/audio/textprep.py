"""Turn a script line into what a TTS engine should read: pronunciation respellings, symbols,
currencies and abbreviations spoken as words. Meaning is never changed: only how it is said."""
from __future__ import annotations

import re

SYMBOLS = [
    (re.compile(r"(\d)\s*%"), r"\1 percent"),
    (re.compile(r"₹\s*(\d[\d,.]*)\s*(crore|lakh|cr|billion|million|trillion|bn|mn)?(?![a-z])", re.I),
     lambda m: f"{m.group(1)} {_unit(m.group(2))}rupees"),
    (re.compile(r"\$\s*(\d[\d,.]*)\s*(billion|million|trillion|bn|mn|cr|crore|lakh)?(?![a-z])", re.I),
     lambda m: f"{m.group(1)} {_unit(m.group(2))}dollars"),
    (re.compile(r"€\s*(\d[\d,.]*)\s*(billion|million|trillion|bn|mn|cr|crore|lakh)?(?![a-z])", re.I),
     lambda m: f"{m.group(1)} {_unit(m.group(2))}euros"),
    (re.compile(r"£\s*(\d[\d,.]*)\s*(billion|million|trillion|bn|mn|cr|crore|lakh)?(?![a-z])", re.I),
     lambda m: f"{m.group(1)} {_unit(m.group(2))}pounds"),
    (re.compile(r"(\d)\s*bps\b", re.I), r"\1 basis points"),
    (re.compile(r"(\d)\s*bn\b", re.I), r"\1 billion"),
    (re.compile(r"(\d)\s*mn\b", re.I), r"\1 million"),
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"\bvs\.?\s", re.I), "versus "),
    (re.compile(r"\be\.g\.\s?", re.I), "for example, "),
    (re.compile(r"\bi\.e\.\s?", re.I), "that is, "),
    (re.compile(r"(?<=\d),(?=\d{3}(?!\d))"), ""),                  # 1,200,000 -> 1200000 (all separators)
    (re.compile(r"\s*→\s*"), ", then "),
    (re.compile(r"[“”\"«»]"), ""),                              # quote marks are not spoken
    (re.compile(r"\[\d+\]"), ""),                               # citation markers, if any
]


UNITS = {"bn": "billion", "mn": "million", "cr": "crore"}


def _unit(u: str | None) -> str:
    return (UNITS.get(u.lower(), u) + " ") if u else ""


def tts_text(text: str, pronunciations: dict[str, str] | None = None) -> str:
    out = text
    for name, say in sorted((pronunciations or {}).items(), key=lambda kv: -len(kv[0])):
        if name and say:
            out = re.sub(r"(?<![\w])" + re.escape(name) + r"(?![\w])", say, out)
    for rx, rep in SYMBOLS:
        out = rx.sub(rep, out)
    return " ".join(out.split())
