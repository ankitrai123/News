"""The intelligence brief (Phase 4 output): one Markdown file and one JSON file per run.

For each event: what happened, what is new, timeline and earlier events, agreement and
disagreement (from Phase 3), impact evidence by dimension/sector/India with quoted statements,
figures, causal chains (sourced vs inferred steps), affected groups, readings, uncertainty and
limitations. Then connections between events and new forecasts."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import DIMENSIONS, SECTORS

BASIS = {"reported": "sourced", "inferred": "inference"}


_MD_SPECIAL = re.compile(r"([\\`*_\[\]()<>!#|~{}])")


def _md(text) -> str:
    """Untrusted text (source titles, publisher names, model output) must not become links, HTML
    or formatting in the Markdown file: every Markdown-special character is escaped, newlines
    flattened."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text or "").split()))


def render_brief(run_id: str, now, ranked: list, analyses: dict, debates: dict, sources: dict, brief, forecasts) -> str:
    lines = [f"# Intelligence brief — {now:%Y-%m-%d}", "",
             f"Run `{run_id}`. Statements in quotes are verbatim from retrieved sources; their confidence labels "
             f"are: confirmed, reported, plausible, contested, speculative. Steps marked *inference* are IntelPod's "
             f"reasoning, not reporting.", ""]
    for r in ranked:
        a = analyses.get(r.event_id)
        if a is None:
            continue
        dp = debates.get(r.event_id)
        claims = {c.claim_id: c for c in (dp.claims if dp else [])}
        srcs = sources.get(r.event_id, {})

        def cite(ids) -> str:
            pubs = list(dict.fromkeys(_md(srcs[i].publisher) for i in ids if i in srcs))
            return ", ".join(pubs[:3]) + (f" +{len(pubs) - 3}" if len(pubs) > 3 else "")

        def quote(cid) -> str:
            c = claims.get(cid)
            return f"“{_md(c.text)}” ({c.confidence}; {cite(c.source_ids)})" if c else ""

        lines += [f"## {r.rank}. {_md(r.title)}", ""]
        if a.status in ("skipped", "failed"):
            lines += ["_Not analysed:_ " + " ".join(a.limitations), ""]
            continue
        ctx = a.context
        lines += [f"**What happened** ({_md(ctx.what_happened_basis)}): {_md(ctx.what_happened)}", ""]
        lines += ["**What is new:** " + " ".join(_md(x) for x in ctx.whats_new), ""]
        if ctx.timeline:
            lines.append("**Timeline:**")
            for t in ctx.timeline:
                when = t.when.strftime("%Y-%m-%d") if t.when else "undated"
                label = {"earlier_event": "earlier event", "background_source": _md(t.publisher or "source"),
                         "this_event": "this event"}[t.kind]
                lines.append(f"- {when} — {_md(t.text)} _({label})_")
            lines.append("")
        if ctx.earlier_events:
            lines.append("**Earlier related events** (shared actors; context, not demonstrated causes): "
                         + "; ".join(f"{_md(p.title)} ({_md(', '.join(p.shared))})" for p in ctx.earlier_events))
            lines.append("")
        if dp and dp.status not in ("skipped", "failed"):
            agreed = [quote(c) for c in dp.consensus[:3]]
            if agreed:
                lines += ["**Broadly agreed:** " + " ".join(agreed), ""]
            if dp.contested:
                lines += ["**Disputed:** " + " ".join(quote(c) for c in dp.contested[:3]), ""]
            for q in dp.questions:
                lines.append(f"**Debated — {_md(q.question)}** " + "; ".join(
                    f"{_md(p.label)} ({p.evidence_strength})" for p in q.positions))
            if dp.no_disagreement_note:
                lines.append(f"_{dp.no_disagreement_note}_")
            if dp.questions or dp.no_disagreement_note:
                lines.append("")
        lines.append("**Impact evidence (what the sources discuss):**")
        for d in a.dimensions[:8]:
            lines.append(f"- {d.label}: {len(d.claim_ids)} statement(s) — " + quote(d.claim_ids[0]))
        if a.sectors:
            lines.append("- Sectors mentioned: " + ", ".join(f"{s.label} ({len(s.source_ids)} source(s))"
                                                            for s in a.sectors[:8]))
        if a.not_discussed:
            lines.append("- Not discussed by any source: " + ", ".join(a.not_discussed))
        lines.append("")
        if a.india:
            lines.append("**India:** " + (_md(a.india_summary) + " " if a.india_summary else "")
                         + " ".join(quote(c) for c in a.india.claim_ids[:2]))
            lines.append("")
        if a.figures:
            lines.append("**Figures stated by sources:** " + "; ".join(
                f"{_md(f.figure)} ({f.kind}, {f.confidence}; {cite(f.source_ids)})" for f in a.figures[:8]))
            lines.append("")
        for ch in a.chains:
            lines.append(f"**Effect chain — {_md(ch.title)}** ({ch.horizon.replace('_', ' ')}, {ch.direction}, "
                         f"{ch.status}, confidence {ch.confidence}{', India' if ch.india else ''})")
            for i, s in enumerate(ch.steps, 1):
                order = "first-order" if i == 1 else ("second-order" if i == 2 else "further")
                src = f" [{cite([x for c in s.claim_ids if c in claims for x in claims[c].source_ids])}]" if s.claim_ids else ""
                lines.append(f"{i}. {_md(s.text)} _({order}; {BASIS[s.basis]}; {s.confidence})_{src}")
            lines.append("")
        groups = {"benefits": "Likely beneficiaries", "harmed": "Adversely affected", "unintended": "Unintended consequences"}
        for eff, title in groups.items():
            items = [x for x in a.actors if x.effect == eff]
            if items:
                lines.append(f"**{title}:** " + "; ".join(f"{_md(x.group)} — {_md(x.reason)} _({BASIS[x.basis]})_"
                                                         for x in items))
        for it in a.interpretations:
            lines.append(f"**Strongest {it.stance} reading:** {_md(it.summary)} [{cite(it.source_ids)}]")
        if a.interpretation_note:
            lines.append(f"_{a.interpretation_note}_")
        if dp and dp.uncertainties:
            lines += ["", "**Uncertain:** " + " ".join(_md(u) for u in dp.uncertainties)]
        if dp and dp.would_change:
            lines.append("**Would change the assessment:** " + "; ".join(_md(u) for u in dp.would_change))
        if a.limitations:
            lines += ["", "_Limitations:_ " + " ".join(a.limitations)]
        lines.append("")
    if brief and brief.links:
        lines += ["## Connections between events", ""]
        for link in brief.links:
            head = f"- **{_md(link.title_a)}** ↔ **{_md(link.title_b)}** — {link.kind.replace('_', ' ')}"
            if link.channels:
                head += f" (channels: {', '.join(link.channels)})"
            lines.append(head)
            if link.explanation:
                lines.append(f"  - {_md(link.explanation)} _(worded by {_md(link.explanation_by)}, checked against the quotes)_")
            for e in link.evidence[:2]:
                lines.append(f"  - “{_md(e.text)}” — {_md(e.publisher)}")
            if link.kind == "shared_driver":
                lines.append(f"  - _{link.note}_")
        lines.append("")
    if forecasts:
        lines += ["## New forecasts recorded in the ledger", ""]
        for f in forecasts:
            lines.append(f"- “{_md(f.text)}” — {_md(f.speaker or f.publisher or 'unknown')}"
                         + (f", timeframe: {_md(f.timeframe)}" if f.timeframe else ", no timeframe stated")
                         + f" · `{f.forecast_id}`")
        lines.append("")
    if brief and brief.limitations:
        lines += ["_Run limitations:_ " + " ".join(brief.limitations), ""]
    lines += ["---", "_Built only from retrieved sources and IntelPod's own records. Figures are quoted, never "
              "estimated. Forecasts are statements about the future, not facts._"]
    return "\n".join(lines) + "\n"


def write_brief(out_dir: Path, run_id: str, now, ranked, analyses, debates, sources, brief, forecasts) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "intelligence_brief.md"
    md.write_text(render_brief(run_id, now, ranked, analyses, debates, sources, brief, forecasts), encoding="utf-8")
    js = out_dir / "intelligence_brief.json"
    js.write_text(json.dumps({
        "run_id": run_id, "generated_for": now.isoformat(),
        "dimensions": DIMENSIONS, "sectors": SECTORS,
        "events": [analyses[r.event_id].model_dump(mode="json") for r in ranked if r.event_id in analyses],
        "links": [link.model_dump(mode="json") for link in (brief.links if brief else [])],
        "new_forecasts": [f.model_dump(mode="json") for f in forecasts],
        "limitations": brief.limitations if brief else [],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"brief": md, "brief_json": js}
