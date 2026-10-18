"""Readable Phase 1 output: Markdown briefing + machine-readable JSON."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..labels import date_source_label, flag_label, plural, topic_label
from ..schemas import EXACT_DATE_SOURCES, Article, RankedEvent

if TYPE_CHECKING:
    from .runner import RunContext


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown"


def render_research(pkg, results) -> list[str]:
    """Markdown for one event's research package (selected sources + limitations)."""
    from ..research.models import QUERY_TYPE_LABELS
    lines = [f"**Research ({len(results)} source{'s' if len(results) != 1 else ''}, "
             f"{pkg.window_start:%Y-%m-%d} to {pkg.window_end:%Y-%m-%d}):**"]
    for r in results:
        when = r.published_at.strftime("%Y-%m-%d") if r.published_at else "undated"
        if r.date_source == "gdelt_seendate":
            when += " (first seen)"
        q = r.credibility_assessment.score
        lines.append(f"- [{r.title}]({r.url}) — {r.publisher}, {when} · {r.content_type}"
                     + (f" · quality {q:.2f}" if q is not None else "")
                     + f" · serves: {', '.join(QUERY_TYPE_LABELS[x].lower() for x in r.query_types[:3])}")
        for n in r.evidence_notes[:2]:
            prefix = "IntelPod note" if n.kind == "model_note" else "extract"
            lines.append(f"  - _{prefix}:_ {'“' + n.text + '”' if n.kind == 'extract' else n.text}")
    if pkg.limitations:
        lines.append("")
        lines.append("_Limitations:_ " + " ".join(pkg.limitations))
    return lines + [""]


def render_debate(pkg, sources: dict) -> list[str]:
    """Markdown for one event's debate package: agreement, disagreement, uncertainty."""
    claims = {c.claim_id: c for c in pkg.claims}
    if pkg.status in ("skipped", "failed"):
        return ["**Viewpoints and evidence:** not available. " + " ".join(pkg.limitations), ""]

    def cite(ids) -> str:
        names = list(dict.fromkeys(sources[i].publisher for i in ids if i in sources))
        return ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")

    def claim_line(cid) -> str:
        c = claims[cid]
        extra = f"; contradicted by {cite(c.contradicted_by)}" if c.contradicted_by else ""
        return f"- “{c.text}” — {cite(c.source_ids)} · _{c.confidence}: {c.confidence_reason}{extra}_"

    lines = [f"**Viewpoints and evidence** (overall confidence: {pkg.overall_confidence} — {pkg.confidence_reason}):"]
    for title, ids in (("Broadly agreed", pkg.consensus[:5]), ("Contested", pkg.contested[:5]),
                       ("Single-source (unverified)", pkg.unverified[:4]), ("Forecasts", pkg.forecasts[:3])):
        if ids:
            lines += [f"_{title}:_"] + [claim_line(i) for i in ids]
    for q in pkg.questions:
        lines.append(f"_Question ({q.disagreement_type}):_ {q.question}")
        for p in q.positions:
            if p.found:
                lines.append(f"- **{p.label}** ({p.evidence_strength}: {p.strength_reason}) — {p.summary} "
                             f"[{cite(p.source_ids)}]" + (f"; challenged by {cite(p.challenged_by)}" if p.challenged_by else ""))
            else:
                lines.append(f"- {p.label}: no retrieved source argues this")
        if q.asymmetry:
            lines.append(f"  - _{q.asymmetry}_")
    if pkg.no_disagreement_note:
        lines.append(f"_{pkg.no_disagreement_note}_")
    if pkg.uncertainties:
        lines.append("_Uncertain:_ " + " ".join(pkg.uncertainties))
    if pkg.would_change:
        lines.append("_Would change the assessment:_ " + "; ".join(pkg.would_change))
    d = pkg.diversity
    lines.append(f"_Source mix:_ {d.sources} sources; countries {dict(d.countries)}; types {dict(d.content_types)}; "
                 f"{d.registry_coverage}.")
    if d.gaps:
        lines.append("_Coverage gaps:_ " + " ".join(d.gaps))
    if pkg.limitations:
        lines.append("_Limitations:_ " + " ".join(pkg.limitations[:6]))
    return lines + [""]


def render_markdown(ctx: "RunContext", selected: list[RankedEvent], ranked: list[RankedEvent],
                    candidates: dict[str, dict], articles: dict[str, Article], research: dict | None = None,
                    debates: dict | None = None) -> str:
    research = research or {}
    debates = debates or {}
    ingest = ctx.outputs.get("ingest", {})
    cluster = ctx.outputs.get("cluster", {})
    lines = [f"# Daily event selection — {ctx.now:%Y-%m-%d}", "",
             f"Run `{ctx.run_id}` · window: last {ctx.params.get('lookback_hours')}h ending {_fmt(ctx.now)}", ""]
    provs = ingest.get("providers", {})
    if provs:
        lines.append("**Sources:** " + "; ".join(f"{k}: {v.get('ok', 0)}/{v.get('requests', 0)} requests ok, "
                                                 f"{v.get('normalized', 0)} articles" for k, v in provs.items()))
    lines.append(f"**Articles considered:** {cluster.get('articles', '?')} → **events:** {cluster.get('events', '?')} "
                 f"({cluster.get('multi_article_events', '?')} with 2+ reports) → **selected:** {len(selected)}")
    if cluster.get("embeddings"):
        lines.append(f"**Embeddings:** {cluster['embeddings']}")
    review = ctx.outputs.get("review") or {}
    if review.get("status") == "completed":
        lines.append(f"**Model review:** {review.get('reviewed')} of {review.get('requested')} top events reviewed by "
                     f"`{', '.join(review.get('models') or [review.get('model')])}` ({review.get('provider')}); "
                     f"scores blend keyword analysis and the model. "
                     f"{review.get('outliers_removed', 0)} off-topic reports removed; "
                     f"{review.get('titles_rejected', 0) + review.get('summaries_rejected', 0)} model texts rejected "
                     f"for naming facts absent from the reports.")
    elif review:
        lines.append(f"**Model review:** not used ({review.get('reason', review.get('status'))}); keyword ranking only.")
    for name, v in provs.items():
        if v.get("note"):
            lines.append(f"> {name}: {v['note']}")
    for w in ingest.get("warnings", []):
        lines.append(f"> ⚠️ {w}")
    if len(selected) < 5:
        lines.append("> Fewer than five events met the threshold — a quiet day, or limited source coverage.")
    lines.append("")
    for r in selected:
        cand = candidates.get(r.event_id, {})
        lines += [f"## {r.rank}. {r.title}", "",
                  f"**Importance {r.importance}/100** · {topic_label(r.topic, r.subtopic)}"
                  + f" · countries: {', '.join(r.countries) or '—'} · {plural(r.article_count, 'report')}, "
                  f"{plural(r.independent_sources, 'independent source')}"
                  + (f" · cluster confidence {r.cluster_confidence}" if r.cluster_confidence is not None else " · single report"),
                  ""]
        if r.headline:
            lines += [f"_Title written by {r.review.model if r.review else 'the model'} from the reports below; "
                      f"lead publisher headline: “{r.headline}”_", ""]
        if r.summary:
            lines += [f"**Summary ({r.review.model if r.review else 'model'}, checked against the reports):** {r.summary}", ""]
        if r.description:
            lines += [f"> {r.description}", ""]
        lines += [f"**Why selected:** {r.rationale}", "", "| Criterion | Score | Weight | Evidence |", "|---|---|---|---|"]
        for c in r.components:
            lines.append(f"| {c.name} | {c.value:.2f} | {c.weight} | {'; '.join(c.evidence)} |")
        if r.flags:
            lines.append(f"\nNotes: {', '.join(flag_label(f) for f in r.flags)}")
        lines += ["", "**Reports:**"]
        for aid in cand.get("article_ids", [])[:12]:
            a = articles.get(aid)
            if a:
                when = _fmt(a.published_at) + ("" if a.date_source in EXACT_DATE_SOURCES
                                               else f" ({date_source_label(a.date_source)})")
                lines.append(f"- [{a.title}]({a.source_url}) — {a.source_name}, {when}")
        extra = len(cand.get("article_ids", [])) - 12
        if extra > 0:
            lines.append(f"- … and {extra} more")
        lines.append("")
        if r.event_id in research:
            lines += render_research(*research[r.event_id])
        if r.event_id in debates:
            srcs = {x.result_id: x for x in (research.get(r.event_id) or (None, []))[1]}
            lines += render_debate(debates[r.event_id], srcs)
    runners = [r for r in ranked if not r.selected][:10]
    if runners:
        lines += ["## Next in line (not selected)", ""]
        for r in runners:
            lines.append(f"- **{r.importance}** — {r.title} _({r.not_selected_reason or r.rationale.split(':', 1)[-1].split('.')[0].strip()})_")
    research_out = ctx.outputs.get("research") or {}
    if research_out.get("attribution"):
        lines += ["", f"_{research_out['attribution']}_"]
    lines += ["", "---", "_Generated from public feed/API metadata and short extracts only. Titles and short descriptions belong to their "
              "publishers; follow the links for full reporting. Scores are heuristic and explained above._"]
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, ctx: "RunContext", selected: list[RankedEvent], ranked: list[RankedEvent],
                 candidates: dict[str, dict], articles: dict[str, Article], research: dict | None = None,
                 debates: dict | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "phase1_events.md"
    md.write_text(render_markdown(ctx, selected, ranked, candidates, articles, research, debates), encoding="utf-8")
    js = out_dir / "phase1_events.json"
    payload = {
        "run_id": ctx.run_id, "generated_for": ctx.now.isoformat(),
        "selected": [r.model_dump(mode="json") | {"articles": [
            articles[a].model_dump(mode="json", include={"article_id", "title", "source_name", "source_url",
                                                         "published_at", "date_source", "description"})
            for a in candidates.get(r.event_id, {}).get("article_ids", []) if a in articles]} for r in selected],
        "not_selected": [r.model_dump(mode="json", include={"event_id", "title", "importance", "rationale", "topic"})
                         for r in ranked if not r.selected],
    }
    js.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    paths = {"markdown": md, "json": js}
    if research:
        rj = out_dir / "research.json"
        rj.write_text(json.dumps({eid: {"package": pkg.model_dump(mode="json"),
                                        "sources": [r.model_dump(mode="json") for r in results]}
                                  for eid, (pkg, results) in research.items()}, indent=2, ensure_ascii=False),
                      encoding="utf-8")
        paths["research"] = rj
    if debates:
        dj = out_dir / "debate.json"
        dj.write_text(json.dumps({eid: p.model_dump(mode="json") for eid, p in debates.items()}, indent=2,
                                 ensure_ascii=False), encoding="utf-8")
        paths["debate"] = dj
    return paths
