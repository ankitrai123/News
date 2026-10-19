"""Turn a script line into what a TTS engine should read: pronunciation respellings, symbols,
currencies and abbreviations spoken as words. Meaning is never changed: only how it is said."""
from __future__ import annotations

import re

SYMBOLS = [
    (re.compile(r"(\d)\s*%"), r"\1 percent"),
    (re.compile(r"₹\s*(\d[\d,.]*)\s*(crore|lakh|billion|million|trillion)?", re.I),
     lambda m: f"{m.group(1)} {m.group(2) + ' ' if m.group(2) else ''}rupees"),
    (re.compile(r"\$\s*(\d[\d,.]*)\s*(billion|million|trillion)?", re.I),
     lambda m: f"{m.group(1)} {m.group(2) + ' ' if m.group(2) else ''}dollars"),
    (re.compile(r"€\s*(\d[\d,.]*)\s*(billion|million|trillion)?", re.I),
     lambda m: f"{m.group(1)} {m.group(2) + ' ' if m.group(2) else ''}euros"),
    (re.compile(r"£\s*(\d[\d,.]*)\s*(billion|million|trillion)?", re.I),
     lambda m: f"{m.group(1)} {m.group(2) + ' ' if m.group(2) else ''}pounds"),
    (re.compile(r"(\d)\s*bps\b", re.I), r"\1 basis points"),
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"\bvs\.?\s", re.I), "versus "),
    (re.compile(r"\be\.g\.\s?", re.I), "for example, "),
    (re.compile(r"\bi\.e\.\s?", re.I), "that is, "),
    (re.compile(r"(\d),(\d{3})"), r"\1\2"),                     # 1,200 -> 1200 (engines read it better)
    (re.compile(r"\s*→\s*"), ", then "),
    (re.compile(r"[“”\"«»]"), ""),                              # quote marks are not spoken
    (re.compile(r"\[\d+\]"), ""),                               # citation markers, if any
]


def tts_text(text: str, pronunciations: dict[str, str] | None = None) -> str:
    out = text
    for name, say in sorted((pronunciations or {}).items(), key=lambda kv: -len(kv[0])):
        if name and say:
            out = re.sub(r"(?<![\w])" + re.escape(name) + r"(?![\w])", say, out)
    for rx, rep in SYMBOLS:
        out = rx.sub(rep, out)
    return " ".join(out.split())
