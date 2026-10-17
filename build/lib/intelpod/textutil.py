"""Deterministic text processing: HTML stripping, URL canonicalization, title
normalization, tokenization, and taxonomy/gazetteer matching."""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(value: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", value or ""))).strip()


def stable_hash(*parts: str, length: int = 16) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:length]


# --- URLs -----------------------------------------------------------------
_TRACKING_PREFIXES = ("utm_", "at_", "ns_", "mc_", "pk_", "hsa_")
_TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "cmpid", "ito", "ocid", "smid",
                  "src", "ref", "ref_src", "referrer", "cid", "mbid", "rss", "feed", "partner", "taid", "guccounter",
                  "guce_referrer", "guce_referrer_sig", "sr_share", "share", "s_cid", "spm", "traffic_source", "xtor"}


def canonicalize_url(url: str) -> str:
    """Normalise a URL for identity: https, lowercase host without www, no fragment,
    no tracking params, sorted query, no trailing slash, no trailing /amp."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("amp.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/+", "/", parts.path or "/")
    path = re.sub(r"/amp/?$", "", path)
    path = path.rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_KEYS]
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def domain_of(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host or None


# --- titles ---------------------------------------------------------------
_TITLE_SEPARATORS = re.compile(r"\s+(?:[-|–—:]|::)\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def strip_publisher_suffix(title: str, source_name: str | None, domain: str | None) -> str:
    """'Chip curbs tighten - BBC News' -> 'Chip curbs tighten' when the suffix names the source."""
    segments = _TITLE_SEPARATORS.split(title)
    if len(segments) < 2:
        return title
    last = segments[-1].lower()
    hints = set()
    if source_name:
        hints.add(source_name.lower())
        hints.update(w for w in source_name.lower().split() if len(w) > 3)
    if domain:
        hints.add(domain.split(".")[0].lower())
    if len(last.split()) <= 5 and any(h and h in last for h in hints):
        return title[: title.lower().rfind(segments[-1].lower())].rstrip(" -|–—:")
    return title


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title).lower()
    t = re.sub(r"['’‘`]", "", t)  # "Fed's" -> "feds" (keeps words together)
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


# --- tokenization ---------------------------------------------------------
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having
he her here hers him his how i if in into is it its itself just me more most my new no nor not now of off on
once only or other our out over own same says said she should so some such than that the their them then
there these they this those through to too under until up us very was we were what when where which while
who whom why will with would you your amid after says say week year years day days today yesterday tomorrow
news live update updates latest report reports reported first last one two three back get gets set sets make
makes made take takes amid against via per vs mr ms dr
""".split())

_TOKEN = re.compile(r"[a-z0-9][a-z0-9']*[a-z0-9]|[a-z0-9]")  # hyphens split words: "china-made" -> china, made


def stem(word: str) -> str:
    """Tiny suffix stripper: folds plurals and common verb forms so that
    agree/agrees/agreed, price/prices and raise/raised/raising share one stem."""
    for suffix, repl in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "es" and not word.endswith(("ses", "xes", "zes", "ches", "shes")):
                continue
            if suffix == "s" and word.endswith(("ss", "us", "is")):
                continue
            word = word[: -len(suffix)] + repl
            break
    if len(word) >= 4 and word.endswith("e"):
        word = word[:-1]
    return word


def tokenize(text: str, synonyms: dict[str, str] | None = None, exclude: frozenset[str] = frozenset()) -> list[str]:
    synonyms = synonyms or {}
    out = []
    for raw in _TOKEN.findall(unicodedata.normalize("NFKC", text).lower().replace("’", "'")):
        raw = raw.strip("'-").removesuffix("'s")
        if not raw or raw in STOPWORDS or raw in exclude or raw.isdigit() or len(raw) < 2:
            continue
        tok = synonyms.get(raw, raw)
        tok = synonyms.get(stem(tok), stem(tok))
        if tok not in STOPWORDS:
            out.append(tok)
    return out


# --- taxonomy ---------------------------------------------------------------
def _phrase_regex(terms: list[str]) -> re.Pattern:
    alts = sorted({t.lower() for t in terms}, key=len, reverse=True)
    return re.compile(r"(?<![\w])(" + "|".join(re.escape(a) for a in alts) + r")(?![\w])", re.I)


@dataclass
class Taxonomy:
    topics: dict[str, list[str]]
    india_terms: list[str]
    significance: dict[str, list[str]]
    soft_news: list[str]
    synonyms: dict[str, str]
    figurative_phrases: list[str] = field(default_factory=list)
    phrase_synonyms: dict[str, str] = field(default_factory=dict)
    _patterns: dict[str, re.Pattern] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for name, terms in self.topics.items():
            self._patterns[f"topic:{name}"] = _phrase_regex(terms)
        for name, terms in self.significance.items():
            self._patterns[f"sig:{name}"] = _phrase_regex(terms)
        self._patterns["india"] = _phrase_regex(self.india_terms)
        self._patterns["soft"] = _phrase_regex(self.soft_news)
        self._figurative = _phrase_regex(self.figurative_phrases) if self.figurative_phrases else None
        self._phrases = _phrase_regex(list(self.phrase_synonyms)) if self.phrase_synonyms else None

    def fold_phrases(self, text: str) -> str:
        """'EU tariffs on electric vehicles' -> 'EU tariffs on ev' (for clustering only)."""
        if self._phrases is None:
            return text
        return self._phrases.sub(lambda m: f" {self.phrase_synonyms[m.group(0).lower()]} ", text)

    LITERAL_DIMENSIONS = frozenset({"topic:geopolitics", "topic:defense", "sig:geopolitical"})

    def _match(self, key: str, text: str) -> list[str]:
        if self._figurative is not None and key in self.LITERAL_DIMENSIONS:
            text = self._figurative.sub(" ", text)
        return [m.lower() for m in self._patterns[key].findall(text)]

    def topic_hits(self, text: str) -> dict[str, list[str]]:
        hits = {name: self._match(f"topic:{name}", text) for name in self.topics}
        return {k: v for k, v in hits.items() if v}

    def significance_hits(self, dimension: str, text: str) -> list[str]:
        return self._match(f"sig:{dimension}", text)

    def india_hits(self, text: str) -> list[str]:
        return self._match("india", text)

    def soft_hits(self, text: str) -> list[str]:
        return self._match("soft", text)


def load_taxonomy(path: Path) -> Taxonomy:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Taxonomy(
        topics={k: [str(t) for t in v] for k, v in (data.get("topics") or {}).items()},
        india_terms=[str(t) for t in data.get("india_terms") or []],
        significance={k: [str(t) for t in v] for k, v in (data.get("significance") or {}).items()},
        soft_news=[str(t) for t in data.get("soft_news") or []],
        synonyms={str(k).lower(): str(v).lower() for k, v in (data.get("synonyms") or {}).items()},
        figurative_phrases=[str(t) for t in data.get("figurative_phrases") or []],
        phrase_synonyms={str(k).lower(): str(v).lower() for k, v in (data.get("phrase_synonyms") or {}).items()},
    )


# --- countries ----------------------------------------------------------------
# name/demonym/capital/leader-shorthand -> canonical country (or bloc). Deliberately
# compact; extend as needed. Ambiguous words (e.g. "Georgia", "Jordan") are omitted
# or require exact multi-word forms.
_COUNTRY_ALIASES: dict[str, list[str]] = {
    "United States": ["united states", "u.s.", "us", "usa", "american", "americans", "washington",
                      "white house", "pentagon", "biden administration", "trump administration"],
    "China": ["china", "chinese", "beijing", "prc", "xi jinping"],
    "India": ["india", "indian", "indians", "new delhi", "modi"],
    "Russia": ["russia", "russian", "russians", "moscow", "kremlin", "putin"],
    "Ukraine": ["ukraine", "ukrainian", "ukrainians", "kyiv", "kiev", "zelensky", "zelenskyy"],
    "European Union": ["european union", "eu", "brussels", "european commission"],
    "United Kingdom": ["united kingdom", "uk", "britain", "british", "london", "downing street"],
    "Germany": ["germany", "german", "berlin"],
    "France": ["france", "french", "paris", "macron"],
    "Italy": ["italy", "italian", "rome"],
    "Japan": ["japan", "japanese", "tokyo"],
    "South Korea": ["south korea", "south korean", "seoul"],
    "North Korea": ["north korea", "north korean", "pyongyang"],
    "Taiwan": ["taiwan", "taiwanese", "taipei"],
    "Israel": ["israel", "israeli", "israelis", "jerusalem", "netanyahu", "tel aviv"],
    "Palestinian Territories": ["gaza", "palestinian", "palestinians", "west bank", "hamas"],
    "Iran": ["iran", "iranian", "tehran"],
    "Saudi Arabia": ["saudi arabia", "saudi", "riyadh"],
    "United Arab Emirates": ["uae", "united arab emirates", "emirati", "abu dhabi", "dubai"],
    "Qatar": ["qatar", "qatari", "doha"],
    "Turkey": ["turkey", "turkish", "ankara", "erdogan", "türkiye"],
    "Egypt": ["egypt", "egyptian", "cairo"],
    "Pakistan": ["pakistan", "pakistani", "islamabad"],
    "Bangladesh": ["bangladesh", "bangladeshi", "dhaka"],
    "Sri Lanka": ["sri lanka", "sri lankan", "colombo"],
    "Nepal": ["nepal", "nepali", "nepalese", "kathmandu"],
    "Afghanistan": ["afghanistan", "afghan", "kabul", "taliban"],
    "Indonesia": ["indonesia", "indonesian", "jakarta"],
    "Vietnam": ["vietnam", "vietnamese", "hanoi"],
    "Philippines": ["philippines", "philippine", "filipino", "manila"],
    "Australia": ["australia", "australian", "canberra"],
    "Canada": ["canada", "canadian", "ottawa"],
    "Mexico": ["mexico", "mexican"],
    "Brazil": ["brazil", "brazilian", "brasilia"],
    "Argentina": ["argentina", "argentine", "buenos aires"],
    "Venezuela": ["venezuela", "venezuelan", "caracas"],
    "South Africa": ["south africa", "south african", "pretoria", "johannesburg"],
    "Nigeria": ["nigeria", "nigerian", "abuja"],
    "Ethiopia": ["ethiopia", "ethiopian", "addis ababa"],
    "Sudan": ["sudan", "sudanese", "khartoum"],
    "Syria": ["syria", "syrian", "damascus"],
    "Iraq": ["iraq", "iraqi", "baghdad"],
    "Yemen": ["yemen", "yemeni", "houthi", "houthis"],
    "Lebanon": ["lebanon", "lebanese", "beirut", "hezbollah"],
    "Poland": ["poland", "polish", "warsaw"],
    "Netherlands": ["netherlands", "dutch", "the hague", "amsterdam"],
    "Switzerland": ["switzerland", "swiss", "geneva"],
    "Singapore": ["singapore", "singaporean"],
    "Malaysia": ["malaysia", "malaysian", "kuala lumpur"],
    "Thailand": ["thailand", "thai", "bangkok"],
    "Myanmar": ["myanmar", "burmese"],
    "Hong Kong": ["hong kong"],
    "Spain": ["spain", "spanish", "madrid"],
    "Greece": ["greece", "greek", "athens"],
    "Hungary": ["hungary", "hungarian", "budapest", "orban"],
    "Belarus": ["belarus", "belarusian", "minsk"],
    "Kenya": ["kenya", "kenyan", "nairobi"],
    "Chile": ["chile", "chilean", "santiago"],
    "Colombia": ["colombia", "colombian", "bogota"],
    "Norway": ["norway", "norwegian", "oslo"],
    "Sweden": ["sweden", "swedish", "stockholm"],
    "Denmark": ["denmark", "danish", "copenhagen", "greenland"],
}
# Aliases that are also common lowercase words: only match when capitalised in source text.
_CASE_SENSITIVE = {"us", "uk", "eu", "uae", "prc", "u.s."}


@lru_cache(maxsize=1)
def _country_patterns() -> tuple[re.Pattern, re.Pattern, dict[str, str]]:
    lookup: dict[str, str] = {}
    insensitive, sensitive = [], []
    for country, aliases in _COUNTRY_ALIASES.items():
        for alias in aliases:
            lookup[alias.lower()] = country
            (sensitive if alias in _CASE_SENSITIVE else insensitive).append(alias)
    pat_i = _phrase_regex(insensitive)
    alts = sorted({a.upper() for a in sensitive}, key=len, reverse=True)
    pat_s = re.compile(r"(?<![\w.])(" + "|".join(re.escape(a) for a in alts) + r")(?![\w])")
    return pat_i, pat_s, lookup


@lru_cache(maxsize=1)
def country_alias_tokens() -> frozenset[str]:
    """Words that only name a country (e.g. 'chinese', 'washington', 'united', 'states').
    Clustering drops them from lexical vectors because countries are compared as entities."""
    generic = {"white", "house", "bank", "west", "south", "north", "new", "street", "commission", "administration",
               "union", "european", "the", "city", "party", "states", "united"}
    words = set()
    for aliases in _COUNTRY_ALIASES.values():
        for alias in aliases:
            parts = [w for w in re.split(r"[\s.]+", alias.lower()) if w]
            words.update(parts if len(parts) == 1 else [w for w in parts if w not in generic])
    # "united states" is the most common multi-word alias; drop both words explicitly.
    return frozenset(words | {"united", "states"})


def find_countries(text: str) -> list[str]:
    pat_i, pat_s, lookup = _country_patterns()
    found = [lookup[m.lower()] for m in pat_i.findall(text)]
    found += [lookup[m.lower()] for m in pat_s.findall(text)]
    seen: dict[str, None] = {}
    for c in found:
        seen.setdefault(c, None)
    return list(seen)


_CAP_PHRASE = re.compile(r"\b([A-Z][a-zA-Z0-9&\.\-]+(?:\s+(?:of\s+|the\s+|de\s+)?[A-Z][a-zA-Z0-9&\.\-]+)*)\b")
_ENTITY_STOP = {"The", "A", "An", "In", "On", "At", "As", "Why", "How", "What", "Who", "When", "Where", "Is",
                "Are", "Will", "Could", "After", "Before", "Amid", "New", "Live", "Breaking", "Watch", "Analysis",
                "Opinion", "Explainer", "Exclusive", "Update", "This", "That", "It", "Its", "From", "For", "With",
                "By", "To", "Of", "And", "Or", "But", "Not", "Over", "Says", "Said", "Mr", "Ms", "Dr", "Here",
                "Inside", "Today", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def find_named_entities(text: str) -> list[str]:
    """Cheap proper-noun phrase extraction (capitalised sequences). Not NER; good enough for overlap."""
    out: dict[str, None] = {}
    for m in _CAP_PHRASE.findall(text):
        words = [w for w in m.split() if w not in _ENTITY_STOP]
        if not words:
            continue
        phrase = " ".join(words).strip(".-")
        if len(phrase) >= 3 and not phrase.isupper() or len(phrase) in range(2, 6) and phrase.isupper():
            out.setdefault(phrase.lower(), None)
    return list(out)
