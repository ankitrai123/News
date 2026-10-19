"""Phase 5 orchestration: plan -> write -> verify -> revise once -> remove what still fails.

The script is built only from Phases 3-4 output. Every line is checked (verify.py); a line that
fails is sent back to the model once with its problems and cited items, re-checked, and removed
if it still fails. Removed lines are listed in the evidence report with the reason, so nothing
unsupported reaches audio silently.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator

from ..debate.analysis import _as_list, _drop_nulls, unsupported_terms

from ..config import Settings
from ..research.model_assist import safe_call
from . import writer
from .models import Episode, FactItem, ScriptLine, VerificationSummary
from .planner import FactBook, plan_episode
from .verify import check_line

log = logging.getLogger(__name__)


class RevisedLine(BaseModel):
    id: str = ""
    text: str = ""
    cites: list[str] = Field(default_factory=list)
    _n = model_validator(mode="before")(classmethod(lambda cls, v: _drop_nulls(v)))

    @field_validator("id", "text", mode="before")
    @classmethod
    def _s(cls, v):
        return v if isinstance(v, str) else ""

    @field_validator("cites", mode="before")
    @classmethod
    def _c(cls, v):
        return [str(x).strip().upper() for x in _as_list(v, split=True) if x is not None and str(x).strip()][:8]


class Revision(BaseModel):
    lines: list[RevisedLine] = Field(default_factory=list)

    @field_validator("lines", mode="before")
    @classmethod
    def _l(cls, v):
        return [x for x in _as_list(v) if isinstance(x, dict)]


REVISE = """Some lines of a podcast script failed fact checks. Rewrite each so it is fully supported
by its cited items: keep it natural and short; remove or hedge whatever is not supported; call
disputed items disputed; phrase forecasts and inferences as possibilities; attribute single-source
items ("according to <publisher>"); never add names or numbers absent from the items. If a line
cannot be supported, return an empty text for it.

{lines}

Return {{"lines": [{{"id": "L3", "text": "...", "cites": ["F2"]}}]}}"""


class ScriptService:
    def __init__(self, settings: Settings, llm_client=None):
        self.settings = settings
        self.cfg = settings.script
        self.llm = llm_client if self.cfg.model_writing else None
        self.model_calls = 0
        self.model_errors: list[dict] = []
        self.models_used: list[str] = []
        self._dead = self.llm is None
        self.frame_fallback = False

    def _call(self, fn, *args):
        if self._dead or self.model_calls >= self.cfg.max_model_calls:
            return None
        self.model_calls += 1
        res, err = safe_call(fn, *args)
        if err:
            self.model_errors.append(err)
            if err.get("fatal"):
                self._dead = True
            return None
        return res

    def build(self, run_id: str, now: datetime, ranked, debates, impacts, sources, links) -> Episode:
        hosts = {"A": self.cfg.host_a, "B": self.cfg.host_b}
        book = FactBook()
        plan = plan_episode(book, ranked, debates, impacts, sources, links, self.cfg)
        ep = Episode(run_id=run_id, episode_id=f"ep-{now:%Y%m%d}-{run_id[-6:]}", hosts=hosts, status="validated",
                     title=f"IntelPod daily briefing — {now:%d %B %Y}", plan=plan, created_at=datetime.now(timezone.utc))
        if not plan.stories:
            ep.status = "skipped"
            ep.limitations.append("No event had enough checked material (claims, positions, impact) for a segment; "
                                  "no script was written rather than one built on thin evidence.")
            return ep
        # Only the planned stories' items exist for this episode: dropped stories' material must not
        # reach the opening, "what to watch" or closing either.
        planned = {f for s in plan.stories for f in s.facts}
        facts = {it.fid: it for it in book.items if it.fid in planned}
        story_fids = {s.event_id: set(s.facts) for s in plan.stories}
        story_fids[None] = planned
        lines: list[ScriptLine] = []
        prev_title = None
        for n, story in enumerate(plan.stories, 1):
            items = [facts[f] for f in story.facts]
            seg = None
            if self.llm is not None:
                res = self._call(self._write_story, story, items, n, len(plan.stories), prev_title, hosts)
                if res is not None:
                    out, model = res
                    seg = writer.to_lines(out, "story", story.event_id, len(lines))
                    prons, gloss = writer.extras(out, " ".join(x.text for x in seg))
                    ep.pronunciations += prons
                    ep.glossary += gloss
                    self.models_used.append(model)
                    seg = self._deepen(story, items, seg, hosts, len(lines))
            if not seg:
                seg = writer.template_story(story, items, n, len(lines))
                if self.llm is not None:
                    ep.limitations.append(f"'{story.title}': model writing failed; template segment used")
            lines += seg
            prev_title = story.title
        frame = self._frame(plan, facts, hosts, len(lines))
        opening = [x for x in frame if x.segment == "opening"]
        lines = opening + lines + [x for x in frame if x.segment in ("watch", "closing")]
        for i, x in enumerate(lines, 1):
            x.line_id = f"L{i}"

        titles = {s.event_id: s.title for s in plan.stories}
        titles[None] = " ".join(s.title for s in plan.stories)
        self._verify(lines, facts, story_fids, hosts, titles)
        failing = [x for x in lines if x.check.status == "unchecked"]
        if failing and self.cfg.revise and self.llm is not None:
            self._revise(failing, facts, story_fids, hosts, titles)
        # A story whose model lines all failed falls back to the template, so it never silently
        # disappears while the opening still previews it.
        for n, story in enumerate(plan.stories, 1):
            seg = [x for x in lines if x.segment == "story" and x.event_id == story.event_id]
            if seg and not any(x.check.status in ("supported", "fixed", "revised") for x in seg):
                tmpl = writer.template_story(story, [facts[f] for f in story.facts], n, len(lines))
                self._verify(tmpl, facts, story_fids, hosts, titles)
                at = lines.index(seg[-1]) + 1
                lines[at:at] = tmpl
                ep.limitations.append(f"'{story.title}': every model line failed the checks; template segment used")
        for i, x in enumerate(lines, 1):
            x.line_id = f"L{i}"
        kept = [x for x in lines if x.check.status in ("supported", "fixed", "revised")]
        removed = [x for x in lines if x.check.status not in ("supported", "fixed", "revised")]
        for x in removed:
            x.check.status = "removed"
        ep.lines, ep.removed = kept, removed
        used = {f for x in kept for f in x.cites}
        ep.facts = [it for it in book.items if it.fid in used or any(it.fid in s.facts for s in plan.stories)]
        ep.written_by = ", ".join(dict.fromkeys(self.models_used)) or "template"
        ep.word_count = sum(len(x.text.split()) for x in kept)
        ep.est_minutes = round(ep.word_count / self.cfg.words_per_minute, 1)
        ep.verification = VerificationSummary(
            lines=len(lines), supported=sum(x.check.status == "supported" for x in kept),
            fixed=sum(x.check.status == "fixed" for x in kept), revised=sum(x.check.status == "revised" for x in kept),
            removed=len(removed),
            problems=[f"{x.line_id} ({x.speaker}): “{x.text[:120]}” — " + "; ".join(x.check.problems[:3]) for x in removed])
        ep.pronunciations = list({p.name: p for p in ep.pronunciations if p.name in " ".join(x.text for x in kept)}.values())
        kept_text = " ".join(x.text for x in kept).lower()
        fact_text = " ".join(it.text for it in facts.values())
        ep.glossary = [g for g in {g.term.lower(): g for g in ep.glossary}.values()
                       if g.term.lower() in kept_text and not unsupported_terms(g.explanation, fact_text + " " + g.term)]
        if self.frame_fallback:
            ep.limitations.append("Model writing of the opening, 'what to watch' and closing failed; template used")
        if ep.written_by == "template":
            ep.limitations.append("No model available: the script quotes verified statements with their sources "
                                  "(template); it is accurate but not conversational.")
        if ep.word_count < 0.6 * self.cfg.words:
            ep.limitations.append(f"Script is {ep.word_count} words, short of the {self.cfg.words}-word target: "
                                  f"there was not enough verified material for a longer episode.")
        if removed:
            ep.limitations.append(f"{len(removed)} line(s) were removed because they could not be supported "
                                  f"(see the evidence report).")
        if any(x.segment == "story" for x in removed) and not any(x.segment == "story" for x in kept):
            ep.status = "partial"
        if ep.written_by != "template" and any("template segment" in lim for lim in ep.limitations):
            ep.status = "partial"
        return ep

    # -- steps ---------------------------------------------------------------------------------
    def _deepen(self, story, items, seg: list[ScriptLine], hosts, start: int) -> list[ScriptLine]:
        """One extra pass when a segment came out well short of its budget and fact-sheet items are
        still undiscussed: longer episodes come from more evidence discussed, not from padding."""
        words = sum(len(x.text.split()) for x in seg)
        used = {f for x in seg for f in x.cites}
        if not seg or words >= 0.75 * story.word_budget or all(it.fid in used for it in items):
            return seg
        msgs = writer.deepen_messages(story, items, seg, hosts, story.word_budget - words)
        res = self._call(lambda: self.llm.chat_json("script_writing", msgs, writer.SegmentOut))
        if res is None:
            return seg
        out, _ = res
        more = writer.to_lines(out, "story", story.event_id, start + len(seg))
        return seg + more
    def _write_story(self, story, items, n, total, prev, hosts):
        out, model = self.llm.chat_json("script_writing", writer.story_messages(story, items, n, total, prev, hosts),
                                        writer.SegmentOut)
        return out, model

    def _frame(self, plan, facts: dict[str, FactItem], hosts, start: int) -> list[ScriptLine]:
        leads = []
        for s in plan.stories:
            first = next((facts[f] for f in s.facts if facts[f].kind == "fact"), facts[s.facts[0]])
            leads.append(first)
        watch = [facts[f] for s in plan.stories for f in s.facts if facts[f].kind in ("forecast", "uncertainty")][:6]
        if self.llm is not None:
            res = self._call(lambda: self.llm.chat_json("script_writing", writer.frame_messages(leads, watch, hosts),
                                                        writer.SegmentOut))
            if res is not None:
                out, model = res
                self.models_used.append(model)
                got = writer.to_lines(out, "frame", None, start)
                if got:
                    return got
            self.frame_fallback = True
        a, b = hosts["A"], hosts["B"]
        # Headlines are unverified text: an uncited line may not carry their numbers.
        previews = [s.title for s in plan.stories if not any(ch.isdigit() for ch in s.title)]
        lines = [ScriptLine(line_id="o1", segment="opening", speaker="A", kind="transition",
                            text=f"Welcome to IntelPod. I'm {a}, with {b}. Today: "
                                 + ("; ".join(previews) if previews else "the day's main stories") + "."),
                 ScriptLine(line_id="o2", segment="opening", speaker="B", kind="transition",
                            text="Everything we say comes from sources listed in the episode notes.")]
        for it in watch[:3]:
            lines.append(ScriptLine(line_id="w", segment="watch", speaker="B", kind="uncertainty",
                                    text=(f"Worth watching, according to {', '.join(it.publishers[:2])}: “{it.text}”"
                                          if it.publishers else f"Still open: {it.text}"), cites=[it.fid]))
        lines.append(ScriptLine(line_id="c", segment="closing", speaker="A", kind="transition",
                                text=f"That's IntelPod for today. Thanks for listening, from {a} and {b}."))
        return lines

    def _verify(self, lines, facts, story_fids, hosts, titles) -> None:
        for x in lines:
            key = x.event_id if x.segment == "story" else None
            check, fixed = check_line(x, facts, story_fids.get(key, set()), [hosts["A"], hosts["B"]], titles.get(key, ""))
            if fixed:
                x.text = fixed
            x.check = check

    def _revise(self, failing, facts, story_fids, hosts, titles) -> None:
        blocks = []
        for x in failing[:30]:
            items = "\n".join(f"   {f} [{facts[f].kind}; {facts[f].confidence}] {facts[f].text}"
                              for f in x.cites if f in facts) or "   (no valid items cited)"
            blocks.append(f"{x.line_id} ({x.speaker}, {x.kind}): {x.text}\n   problems: {'; '.join(x.check.problems)}"
                          f"\n   cited items:\n{items}")
        msgs = [{"role": "system", "content": writer.SYSTEM},
                {"role": "user", "content": REVISE.format(lines="\n".join(blocks))}]
        res = self._call(lambda: self.llm.chat_json("script_writing", msgs, Revision))
        if res is None:
            return
        out, _ = res
        by_id = {x.line_id: x for x in failing}
        seen: set[str] = set()
        for r in out.lines:
            lid = r.id.strip().upper()
            x = by_id.get(lid)
            if x is None or lid in seen or not r.text.strip():
                continue
            seen.add(lid)                       # first revision per line only
            original, problems = x.text, x.check.problems
            trial = x.model_copy(update={"text": " ".join(r.text.split())[:600], "cites": r.cites or x.cites})
            key = x.event_id if x.segment == "story" else None
            check, fixed = check_line(trial, facts, story_fids.get(key, set()), [hosts["A"], hosts["B"]],
                                      titles.get(key, ""))
            if check.status in ("supported", "fixed"):
                x.text, x.cites = fixed or trial.text, trial.cites
                x.check.status, x.check.original = "revised", original
                x.check.problems = problems
