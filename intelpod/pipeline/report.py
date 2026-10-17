"""Readable Phase 1 output: Markdown briefing + machine-readable JSON."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..schemas import EXACT_DATE_SOURCES, Article, RankedEvent

if TYPE_CHECKING:
    from .runner import RunContext


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "unknown"


def render_markdown(ctx: "RunContext", selected: list[RankedEvent], ranked: list[RankedEvent],
                    candidates: dict[str, dict], articles: dict[str, Article]) -> str:
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
                  f"**Importance {r.importance}/100** · topic: `{r.topic}`" + (f" / {r.subtopic}" if r.subtopic else "")
                  + f" · countries: {', '.join(r.countries) or '—'} · {r.article_count} reports, "
                  f"{r.independent_sources} independent publishers"
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
            lines.append(f"\nFlags: {', '.join(r.flags)}")
        lines += ["", "**Reports:**"]
        for aid in cand.get("article_ids", [])[:12]:
            a = articles.get(aid)
            if a:
                when = _fmt(a.published_at) + ("" if a.date_source in EXACT_DATE_SOURCES else f" ({a.date_source})")
                lines.append(f"- [{a.title}]({a.source_url}) — {a.source_name}, {when}")
        extra = len(cand.get("article_ids", [])) - 12
        if extra > 0:
            lines.append(f"- … and {extra} more")
        lines.append("")
    runners = [r for r in ranked if not r.selected][:10]
    if runners:
        lines += ["## Next in line (not selected)", ""]
        for r in runners:
            lines.append(f"- **{r.importance}** — {r.title} _({r.rationale.split(':', 1)[-1].split('.')[0].strip()})_")
    lines += ["", "---", "_Generated from public feed/API metadata only. Titles and short descriptions belong to their "
              "publishers; follow the links for full reporting. Scores are heuristic and explained above._"]
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path, ctx: "RunContext", selected: list[RankedEvent], ranked: list[RankedEvent],
                 candidates: dict[str, dict], articles: dict[str, Article]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "phase1_events.md"
    md.write_text(render_markdown(ctx, selected, ranked, candidates, articles), encoding="utf-8")
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
    return {"markdown": md, "json": js}
