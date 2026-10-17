"""Model-output parsing and grounding checks (provider-independent, no network)."""
from __future__ import annotations

import json
import re

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


_SPECIAL_TOKEN = re.compile(r"<\|[a-z_]{2,20}\|>")


def strip_reasoning(text: str) -> str:
    """Remove reasoning that some models put inside `content`:
    <think>...</think> blocks, everything before a lone </think>, an unterminated
    <think> (truncated output), and stray chat-template tokens such as <|return|>."""
    text = _THINK.sub("", text or "")
    if re.search(r"<think>", text, re.I):
        text = re.split(r"<think>", text, flags=re.I)[0]
    if re.search(r"</think>", text, re.I):
        text = re.split(r"</think>", text, flags=re.I)[-1]
    return _SPECIAL_TOKEN.sub("", text).strip()


def extract_json(text: str) -> dict:
    """Return the first JSON object in model output (handles fences, prose, reasoning tags)."""
    text = strip_reasoning(text)
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        obj = _first_balanced_object(cand)
        if obj is not None:
            return obj
    raise ValueError("no JSON object found in model output")


def _first_balanced_object(text: str) -> dict | None:
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


_NUMBER = re.compile(r"\d[\d,.]*%?")
_CAPWORD = re.compile(r"\b[A-Z][a-zA-Z\-']+\b")
_ALLOWED_CAPS = {"The", "A", "An", "In", "On", "At", "As", "And", "Or", "But", "For", "With", "To", "Of", "By",
                 "After", "Amid", "Over", "From", "New", "This", "That", "Its", "It", "Is", "Are", "Will", "May",
                 "Could", "Says", "Report", "Reports", "Officials", "Government", "Talks", "Deal", "Plan"}


def _norm_number(n: str) -> str:
    return n.replace(",", "").rstrip(".")


def unsupported_terms(generated: str, sources: str) -> list[str]:
    """Numbers and capitalised names in model output that never appear in the source text.

    Used to reject model-written titles/summaries that introduce facts the retrieved
    reports do not contain. Deliberately strict: a false rejection only means we keep
    the publisher's own headline."""
    src_lower = sources.lower()
    src_numbers = {_norm_number(n) for n in _NUMBER.findall(sources)}
    issues: list[str] = []
    for n in _NUMBER.findall(generated):
        if _norm_number(n) not in src_numbers:
            issues.append(n)
    for w in _CAPWORD.findall(generated):
        if w in _ALLOWED_CAPS:
            continue
        stem = w.lower().removesuffix("'s").removesuffix("’s")
        if stem not in src_lower and stem.rstrip("s") not in src_lower:
            issues.append(w)
    seen: dict[str, None] = {}
    for i in issues:
        seen.setdefault(i, None)
    return list(seen)
