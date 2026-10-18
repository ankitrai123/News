"""Phase 5 output files: script, source-linked transcript, episode notes, evidence report."""
from __future__ import annotations

import json
from pathlib import Path

from ..impact.report import _md
from .models import Episode

def _url(u: str) -> str:
    """A URL safe inside Markdown link syntax: characters that could end the link are encoded."""
    return "".join({" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "[": "%5B", "]": "%5D",
                    "\n": "", "\r": ""}.get(ch, ch) for ch in (u or ""))


KIND_NOTE = {"fact": "", "analysis": "analysis", "question": "", "transition": "", "uncertainty": "uncertain",
             "explainer": "explainer, general knowledge"}


def citations(ep: Episode, sources: dict) -> tuple[dict[str, list[int]], list[dict]]:
    """Number every cited source in order of first use, one number per page (the same article
    researched under two events is one source). Returns (line_id -> [n], source list)."""
    facts = {f.fid: f for f in ep.facts}
    order: dict[str, int] = {}          # canonical URL -> number
    first: dict[str, str] = {}          # canonical URL -> result_id shown
    per_line: dict[str, list[int]] = {}
    for x in ep.lines:
        nums = []
        for fid in x.cites:
            for sid in facts[fid].source_ids if fid in facts else []:
                if sid not in sources:
                    continue
                key = sources[sid].canonical_url
                if key not in order:
                    order[key], first[key] = len(order) + 1, sid
                if order[key] not in nums:
                    nums.append(order[key])
        per_line[x.line_id] = nums
    listed = []
    for key, n in sorted(order.items(), key=lambda kv: kv[1]):
        r = sources[first[key]]
        listed.append({"n": n, "result_id": r.result_id, "title": r.title, "publisher": r.publisher, "url": r.url,
                       "published_at": r.published_at.isoformat() if r.published_at else None})
    return per_line, listed


def render_script(ep: Episode, sources: dict) -> str:
    per_line, listed = citations(ep, sources)
    lines = [f"# {_md(ep.title)}", "",
             f"Hosts: **{_md(ep.hosts['A'])}** (anchor) and **{_md(ep.hosts['B'])}** (analyst) · "
             f"{ep.word_count} words · about {ep.est_minutes} minutes · written by {_md(ep.written_by)} · "
             f"verification: {ep.verification.supported} supported, {ep.verification.fixed} fixed, "
             f"{ep.verification.revised} revised, {ep.verification.removed} removed", ""]
    seg = None
    titles = {s.event_id: s.title for s in ep.plan.stories}
    for x in ep.lines:
        key = (x.segment, x.event_id)
        if key != seg:
            seg = key
            head = titles.get(x.event_id) if x.segment == "story" else x.segment.capitalize()
            lines += ["", f"## {_md(head)}", ""]
        refs = "".join(f"[{n}]" for n in per_line.get(x.line_id, []))
        note = KIND_NOTE.get(x.kind, "")
        lines.append(f"**{_md(ep.hosts[x.speaker])}:** {_md(x.text)}" + (f" {refs}" if refs else "")
                     + (f" _({note})_" if note else ""))
    if ep.pronunciations:
        lines += ["", "## Pronunciation notes", ""] + [f"- {_md(p.name)}: _{_md(p.say)}_ (suggestion)" for p in ep.pronunciations]
    if ep.glossary:
        lines += ["", "## Terms explained (general knowledge, not from the sources)", ""] + \
                 [f"- **{_md(g.term)}**: {_md(g.explanation)}" for g in ep.glossary]
    lines += ["", "## Sources", ""] + [f"{s['n']}. [{_md(s['title'])}]({_url(s['url'])}) — {_md(s['publisher'])}"
                                       + (f", {s['published_at'][:10]}" if s['published_at'] else "") for s in listed]
    return "\n".join(lines) + "\n"


def render_notes(ep: Episode, sources: dict) -> str:
    _, listed = citations(ep, sources)
    lines = [f"# Episode notes — {_md(ep.title)}", "", f"About {ep.est_minutes} minutes. Hosts {_md(ep.hosts['A'])} and "
             f"{_md(ep.hosts['B'])}.", "", "## Stories", ""]
    for s in ep.plan.stories:
        lines.append(f"- **{_md(s.title)}** — {_md(s.why)}")
    if ep.plan.dropped:
        lines += ["", "Not covered: " + "; ".join(_md(d) for d in ep.plan.dropped)]
    lines += ["", "## Sources", ""] + [f"{s['n']}. [{_md(s['title'])}]({_url(s['url'])}) — {_md(s['publisher'])}"
                                       for s in listed]
    lines += ["", "## How this episode was made", "",
              f"Script written by {_md(ep.written_by)} from IntelPod's checked research and checked line by line "
              f"against the retrieved sources ({ep.verification.removed} unsupported line(s) removed). Statements "
              f"marked as analysis or uncertain are interpretation, not established fact."]
    if ep.limitations:
        lines += ["", "_Limitations:_ " + " ".join(_md(x) for x in ep.limitations)]
    return "\n".join(lines) + "\n"


def render_evidence(ep: Episode) -> str:
    v = ep.verification
    facts = {f.fid: f for f in ep.facts}
    lines = [f"# Evidence report — {_md(ep.title)}", "",
             f"{v.lines} lines checked: {v.supported} supported as written, {v.fixed} fixed automatically, "
             f"{v.revised} revised by the model and re-checked, {v.removed} removed.", "",
             "Checks: citations, names and numbers against cited statements, verbatim quotations, hedging of "
             "forecasts/inferences, disputed statements flagged, single-source attribution, causal claims.", ""]
    if ep.removed:
        lines += ["## Removed lines", ""] + [f"- {_md(x.line_id)} ({_md(ep.hosts[x.speaker])}): “{_md(x.text)}” — "
                                             f"{_md('; '.join(x.check.problems))}" for x in ep.removed] + [""]
    changed = [x for x in ep.lines if x.check.status in ("fixed", "revised")]
    if changed:
        lines += ["## Changed lines", ""] + [f"- {x.line_id} {x.check.status}: “{_md(x.check.original or '')}” → "
                                             f"“{_md(x.text)}” ({_md('; '.join(x.check.problems))})" for x in changed] + [""]
    lines += ["## Line-by-line support", ""]
    for x in ep.lines:
        sup = "; ".join(f"{f} [{facts[f].kind}, {facts[f].confidence}] “{_md(facts[f].text[:140])}”"
                        for f in x.cites if f in facts) or "no citation (question/transition)"
        lines.append(f"- {x.line_id} {x.check.status}: {sup}")
    return "\n".join(lines) + "\n"


def write_episode(out_dir: Path, ep: Episode, sources: dict) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"script": out_dir / "script.md", "episode_notes": out_dir / "episode_notes.md",
             "evidence_report": out_dir / "evidence_report.md", "transcript": out_dir / "transcript.json"}
    paths["script"].write_text(render_script(ep, sources), encoding="utf-8")
    paths["episode_notes"].write_text(render_notes(ep, sources), encoding="utf-8")
    paths["evidence_report"].write_text(render_evidence(ep), encoding="utf-8")
    per_line, listed = citations(ep, sources)
    paths["transcript"].write_text(json.dumps({
        "episode_id": ep.episode_id, "title": ep.title, "hosts": ep.hosts, "written_by": ep.written_by,
        "lines": [{"id": x.line_id, "speaker": ep.hosts[x.speaker], "role": x.speaker, "segment": x.segment,
                   "kind": x.kind, "text": x.text, "sources": per_line.get(x.line_id, []), "check": x.check.status}
                  for x in ep.lines],
        "sources": listed, "pronunciations": [p.model_dump() for p in ep.pronunciations],
        "verification": ep.verification.model_dump()}, indent=2, ensure_ascii=False), encoding="utf-8")
    return paths
