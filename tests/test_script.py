"""Phase 5: planning, line verification, model writing + revision, template fallback, outputs."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from intelpod.config import ScriptConfig
from intelpod.debate.models import Claim, DebatePackage, Position, Question
from intelpod.impact.models import EventLink, ImpactAnalysis
from intelpod.schemas import RankedEvent
from intelpod.script.models import FactItem, ScriptLine
from intelpod.script.planner import FactBook, plan_episode
from intelpod.script.service import ScriptService
from intelpod.script.verify import check_line
from intelpod.web.app import create_app
from test_debate_units import NOW, res

F = {"F1": FactItem(fid="F1", kind="fact", text="The Reserve Bank of India cut its repo rate to 5.25% on Friday.",
                    confidence="confirmed", event_id="e", publishers=["Reuters", "Mint"]),
     "F2": FactItem(fid="F2", kind="fact", text="Banks said loan demand rose in September.", confidence="reported",
                    event_id="e", publishers=["Mint"]),
     "F3": FactItem(fid="F3", kind="forecast", text="Economists expect inflation to ease next year.",
                    confidence="speculative", event_id="e", publishers=["BBC"]),
     "F4": FactItem(fid="F4", kind="contested", text="Officials said 40 people were killed.", confidence="contested",
                    event_id="e", publishers=["AP"]),
     "F9": FactItem(fid="F9", kind="fact", text="Other story fact.", confidence="confirmed", event_id="x")}
HOSTS = ["Maya", "Arjun"]


def chk(text, cites, kind="fact"):
    line = ScriptLine(line_id="L1", segment="story", event_id="e", speaker="A", text=text, kind=kind, cites=cites)
    return check_line(line, F, {"F1", "F2", "F3", "F4"}, HOSTS)


@pytest.mark.parametrize("text,cites,kind,ok", [
    ("The Reserve Bank of India cut its repo rate to 5.25% on Friday.", ["F1"], "fact", True),
    ("The RBI cut rates to 5.5%.", ["F1"], "fact", False),                      # number and name not in item
    ("Inflation will ease next year.", ["F3"], "fact", False),                  # forecast stated as fact
    ("Economists expect inflation to ease next year.", ["F3"], "fact", True),
    ("Forty people were killed.", ["F4"], "fact", False),                       # disputed, not flagged
    ("Officials said 40 people were killed, though accounts differ.", ["F4"], "fact", True),
    ("The cut led to a surge in borrowing.", ["F1"], "analysis", False),        # causal overreach
    ("Goldman Sachs welcomed the cut.", ["F1"], "fact", False),                 # invented name
    ("So what does this mean for borrowers, Arjun?", [], "question", True),
    ("Lending rose 12% last month.", [], "transition", False),                  # uncited number
    ("As one official put it, \"the cut was overdue\".", ["F1"], "fact", False),  # invented quotation
    ("Repo rates fell.", [], "fact", False),                                    # fact without citation
    ("Other story fact.", ["F9"], "fact", False),                               # another story's item
])
def test_line_checks(text, cites, kind, ok):
    check, _ = chk(text, cites, kind)
    assert (check.status == "supported") == ok, check.problems


def test_single_source_gets_attribution_automatically():
    check, fixed = chk("Loan demand rose in September.", ["F2"])
    assert check.status == "fixed" and fixed == "According to Mint, loan demand rose in September."


# -- planning -------------------------------------------------------------------------------------
def claim(cid, text, kind="factual", confidence="confirmed", sources=("a",)):
    return Claim(claim_id=cid, text=text, kind=kind, confidence=confidence, source_ids=list(sources),
                 independent_sources=len(sources))


def ranked(eid, title, importance, rank):
    return RankedEvent.model_construct(event_id=eid, title=title, importance=importance, rank=rank, selected=True)


def package(eid, claims, consensus, questions=(), forecasts=(), unverified=()):
    return DebatePackage(run_id="r", event_id=eid, event_title=eid, status="completed", claims=claims,
                         consensus=list(consensus), forecasts=list(forecasts), questions=list(questions),
                         unverified=list(unverified), created_at=NOW)


def material():
    rbi = [claim("r1", "The Reserve Bank of India cut its repo rate to 5.25% on Friday.", sources=("a", "b")),
           claim("r2", "Governor Sanjay Malhotra said inflation had eased.", kind="attributed"),
           claim("r3", "Economists expect inflation to ease to 4% next year.", kind="forecast", confidence="speculative")]
    oil = [claim("o1", "Oil prices rose 3% after OPEC+ agreed to cut output.", sources=("c", "d")),
           claim("o2", "Brent crude traded above $90 a barrel.", sources=("c",), confidence="reported")]
    thin = [claim("t1", "A minor event happened.", confidence="reported")]
    q = Question(question="Will the cut help?", disagreement_type="predictive", positions=[
        Position(label="Supports growth", summary="The cut supports growth.", source_ids=["a"], evidence_strength="moderate"),
        Position(label="Inflation risk", summary="Inflation could return.", source_ids=["b"], evidence_strength="weak")])
    debates = {"rbi": package("rbi", rbi, ["r1", "r2"], [q], ["r3"]), "oil": package("oil", oil, ["o1"], unverified=["o2"]),
               "thin": package("thin", thin, [])}
    events = [ranked("oil", "OPEC+ agrees output cut", 80, 1), ranked("thin", "Minor event", 70, 2),
              ranked("rbi", "RBI cuts repo rate", 60, 3)]
    srcs = {e: {s: res(s, f"Pub {s.upper()}", f"title {s}") for s in "abcd"} for e in ("rbi", "oil", "thin")}
    link = EventLink(run_id="r", link_id="l", event_a="rbi", event_b="oil", title_a="RBI", title_b="OPEC",
                     kind="evidenced")
    impacts = {"rbi": ImpactAnalysis(run_id="r", event_id="rbi", event_title="RBI", status="completed", created_at=NOW)}
    return events, debates, impacts, srcs, [link]


def test_plan_selects_supported_stories_and_orders_connected_ones():
    events, debates, impacts, srcs, links = material()
    book = FactBook()
    plan = plan_episode(book, events, debates, impacts, srcs, links, ScriptConfig(max_stories=3))
    ids = [s.event_id for s in plan.stories]
    assert ids == ["oil", "rbi"]                             # strongest first; the connected story follows it
    assert "thin" not in ids and any("Minor event" in d for d in plan.dropped)
    rbi_kinds = {book.get(f).kind for f in plan.stories[1].facts}
    assert {"fact", "position", "forecast"} <= rbi_kinds
    # budget = share of the target, capped at ~150 words per fact-sheet item (no padding)
    assert all(s.word_budget == min((1800 - 300) // 2, 150 * len(s.facts)) for s in plan.stories)


def test_word_budget_caps_number_of_stories():
    events, debates, impacts, srcs, links = material()
    plan = plan_episode(FactBook(), events, debates, impacts, srcs, links,
                        ScriptConfig(target_words=700, min_story_words=300))
    assert len(plan.stories) == 1 and any("depth" in d for d in plan.dropped)


# -- writing with a scripted model -----------------------------------------------------------------
class ScriptModel:
    provider = "fake"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def availability(self):
        return True, "ok"

    def chat_json(self, task, messages, schema):
        assert task == "script_writing"
        user = messages[-1]["content"]
        self.calls.append(user[:40])
        if self.fail:
            err = RuntimeError("model gone")
            err.fatal = True
            raise err
        facts = dict(re.findall(r"^\s*(F\d+) \[[^\]]*\] (.+)$", user, re.M))
        if "failed fact checks" in user:
            out = []
            for lid, body in re.findall(r"^(L\d+) \([AB], \w+\): (.+)$", user, re.M):
                if "invented" in body:
                    out.append({"id": lid, "text": ""})                       # cannot support: give up
                else:
                    fid = re.search(r"(F\d+) \[", user[user.index(lid):]).group(1)
                    out.append({"id": lid, "text": f"It could be that {facts[fid][0].lower()}{facts[fid][1:]}",
                                "cites": [fid]})
            return schema.model_validate({"lines": out}), "fake-writer"
        if "Write three short parts" in user:
            first = next(iter(facts))
            return schema.model_validate({"lines": [
                {"segment": "opening", "speaker": "A", "kind": "transition", "text": "Welcome to IntelPod."},
                {"segment": "watch", "speaker": "B", "kind": "uncertainty", "text": "We will watch closely.",
                 "cites": [first]},
                {"segment": "closing", "speaker": "A", "kind": "transition", "text": "Thanks for listening."}]}), "fake-writer"
        kinds = dict(re.findall(r"^\s*(F\d+) \[(\w+);", user, re.M))
        fact_ids = [f for f, k in kinds.items() if k == "fact"]
        fc = next((f for f, k in kinds.items() if k == "forecast"), None)
        lines = [{"speaker": "A", "kind": "fact", "text": facts[fact_ids[0]], "cites": [fact_ids[0]]},
                 {"speaker": "B", "kind": "question", "text": "Why does it matter?"},
                 {"speaker": "B", "kind": "fact", "text": "Growth jumped 7% as a result, an invented figure.",
                  "cites": [fact_ids[0]]}]
        if fc:
            lines.append({"speaker": "B", "kind": "fact", "text": facts[fc].replace("expect", "say"), "cites": [fc]})
        return schema.model_validate({"lines": lines,
                                      "pronunciations": [{"name": "Malhotra", "say": "mal-HO-tra"},
                                                         {"name": "Nobody Mentioned", "say": "x"}],
                                      "glossary": [{"term": "repo rate", "explanation": "The rate at which the central bank lends to banks."},
                                                   {"term": "basis point", "explanation": "One hundredth of 1%."}]}), "fake-writer"


def build(model, cfg=None):
    from intelpod.config import Settings
    s = Settings()
    if cfg:
        s.script = cfg
    events, debates, impacts, srcs, links = material()
    return ScriptService(s, model).build("run_x", NOW, events, debates, impacts, srcs, links)


def test_model_script_is_verified_revised_and_pruned():
    model = ScriptModel()
    ep = build(model)
    assert ep.status == "validated" and ep.written_by == "fake-writer"
    texts = [x.text for x in ep.lines]
    assert not any("invented" in t for t in texts)                       # removed after failed revision
    assert any("7%" in x.text for x in ep.removed)
    revised = [x for x in ep.lines if x.check.status == "revised"]
    assert revised and all(x.check.original for x in revised)            # unhedged forecast revised and kept
    assert ep.verification.removed == len(ep.removed) >= 1
    assert all(x.check.status in ("supported", "fixed", "revised") for x in ep.lines)
    assert [p.name for p in ep.pronunciations] == ["Malhotra"] or ep.pronunciations == []
    assert [g.term for g in ep.glossary] == ["repo rate"]               # entry with a number dropped
    assert ep.lines[0].segment == "opening" and ep.lines[-1].segment == "closing"
    assert ep.word_count == sum(len(x.text.split()) for x in ep.lines)


def test_fatal_model_error_falls_back_to_template():
    ep = build(ScriptModel(fail=True))
    assert ep.written_by == "template" and ep.lines
    assert all(x.check.status in ("supported", "fixed") for x in ep.lines)
    assert any("template" in lim for lim in ep.limitations)


def test_no_material_means_no_script():
    from intelpod.config import Settings
    ep = ScriptService(Settings(), None).build("r", NOW, [ranked("t", "Thin", 50, 1)], {}, {}, {}, [])
    assert ep.status == "skipped" and not ep.lines and "rather than one built on thin evidence" in ep.limitations[0]


# -- pipeline, files, dashboard, CLI ---------------------------------------------------------------
def test_script_stage_outputs_and_pages(settings, tmp_path, monkeypatch, capsys):
    from test_debate_pipeline import run_pipeline
    settings.llm.enabled = False
    pipe, run_id = run_pipeline(settings, tmp_path)
    out = pipe.repo.stages(run_id)["script"].output
    assert out["status"] == "completed" and out["written_by"] == "template" and out["words"] > 0
    d = settings.data_path / "runs" / run_id
    script = (d / "script.md").read_text()
    assert "**Maya:**" in script and "## Sources" in script and re.search(r"\[1\]", script)
    tj = json.loads((d / "transcript.json").read_text())
    assert tj["lines"] and tj["sources"] and all(line["check"] != "removed" for line in tj["lines"])
    assert "lines checked" in (d / "evidence_report.md").read_text()
    assert "## Stories" in (d / "episode_notes.md").read_text()

    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    page = client.get("/episode")
    assert page.status_code == 200 and 'id="script"' in page.text and "Fact check" in page.text
    assert client.get(f"/episode?run={run_id}").status_code == 200
    assert client.get(f"/api/runs/{run_id}/episode").json()["run_id"] == run_id
    assert client.get("/episode?run=nope").status_code == 404

    from intelpod import cli
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    assert cli.main(["script", "--run-id", run_id, "--refresh"]) == 0
    printed = capsys.readouterr().out
    assert "verification:" in printed and "script:" in printed


def test_same_article_under_two_events_is_one_citation():
    from intelpod.script.models import Episode
    from intelpod.script.report import citations
    a, b = res("x1", "Reuters", "Oil jumps"), res("x2", "Reuters", "Oil jumps")
    b.canonical_url = a.canonical_url
    facts = [FactItem(fid="F1", kind="fact", text="t", event_id="e1", source_ids=["x1"]),
             FactItem(fid="F2", kind="fact", text="t", event_id="e2", source_ids=["x2"])]
    lines = [ScriptLine(line_id="L1", segment="story", speaker="A", text="t", cites=["F1"]),
             ScriptLine(line_id="L2", segment="story", speaker="B", text="t", cites=["F2"])]
    ep = Episode(run_id="r", episode_id="e", title="t", status="validated", facts=facts, lines=lines, created_at=NOW)
    per_line, listed = citations(ep, {"x1": a, "x2": b})
    assert per_line == {"L1": [1], "L2": [1]} and len(listed) == 1


# -- review regressions ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,cites,kind", [
    ("According to BBC, inflation will ease next year.", ["F3"], "fact"),          # attribution is not a hedge
    ("Inflation will ease next year, a risk for savers.", ["F3"], "fact"),
    ("Officials claimed 40 people were killed.", ["F4"], "fact"),                   # "claimed" does not flag dispute
    ("Officials in different districts said 40 people were killed.", ["F4"], "fact"),
    ("Inflation will ease next year, so the cut was a mistake.", ["F3"], "question"),  # not a question
    ("As one official said, 'the cut was overdue and reckless'.", ["F1"], "fact"),  # single-quoted invention
    ("The Reserve Bank of India cut its repo rate to five percent on Friday.", ["F1"], "fact"),
    ("Banks said loan demand rose forty percent last week.", ["F2"], "fact"),
    ("The Reserve Bank of India cut its repo rate by 5.25% on Friday.", ["F1"], "fact"),  # to -> by
    ("The Reserve Bank of India did not cut its repo rate on Friday.", ["F1"], "fact"),   # negation
    ("The cut sparked a borrowing boom.", ["F1"], "analysis"),
])
def test_review_loopholes_closed(text, cites, kind):
    check, _ = chk(text, cites, kind)
    assert check.status != "supported", text


def test_titles_never_support_numbers_or_fact_lines():
    line = ScriptLine(line_id="L1", segment="story", event_id="e", speaker="B", kind="fact",
                      text="Banks said loan demand rose in September after Trump threatened 50% tariffs.", cites=["F2"])
    check, _ = check_line(line, F, {"F1", "F2", "F3", "F4"}, HOSTS, "Trump threatens 50% tariff on India")
    assert check.status != "supported"
    t = ScriptLine(line_id="L2", segment="story", event_id="e", speaker="A", kind="transition",
                   text="Next: Trump threatens a tariff on India.")
    assert check_line(t, F, {"F1"}, HOSTS, "Trump threatens 50% tariff on India")[0].status == "supported"


def test_attribution_rules():
    F["F5"] = FactItem(fid="F5", kind="fact", text="Brent crude traded above $90 per barrel.", confidence="reported",
                       event_id="e", publishers=["Mint"])
    check, fixed = check_line(ScriptLine(line_id="L", segment="story", event_id="e", speaker="A", kind="analysis",
                                         text="Brent crude traded above $90 per barrel.", cites=["F5"]),
                              F, {"F5"}, HOSTS)
    assert check.status == "fixed" and fixed == "According to Mint, Brent crude traded above $90 per barrel."
    del F["F5"]


def test_template_never_quotes_model_text_and_story_needs_a_source():
    from intelpod.script.writer import template_story
    from intelpod.script.models import StoryPlan
    items = [FactItem(fid="F1", kind="fact", text="Exports rose.", confidence="confirmed", event_id="e",
                      claim_ids=["c"], publishers=["Mint"]),
             FactItem(fid="F2", kind="india", text="Indian exporters could face higher costs.", confidence="plausible",
                      event_id="e", publishers=["Mint"], basis="model, checked")]
    lines = template_story(StoryPlan(event_id="e", title="T", facts=["F1", "F2"]), items, 1, 0)
    india = next(x for x in lines if "India" in x.text)
    assert "“" not in india.text and "IntelPod" in india.text and "according to" not in india.text.lower()
    # A story whose only "sourced-looking" item is a model summary is not selected.
    events, debates, impacts, srcs, links = material()
    debates["thin"] = package("thin", [], [])
    impacts["thin"] = ImpactAnalysis(run_id="r", event_id="thin", event_title="t", status="completed", created_at=NOW,
                                     india_summary="Indian exporters could face higher costs.")
    from intelpod.impact.models import DimensionNote
    impacts["thin"].india = DimensionNote(key="india", label="India", claim_ids=[], source_ids=[])
    plan = plan_episode(FactBook(), events, debates, impacts, srcs, links, ScriptConfig())
    assert "thin" not in [s.event_id for s in plan.stories]


def test_frame_lines_cannot_use_dropped_stories():
    from intelpod.config import Settings
    s = Settings()
    s.script = ScriptConfig(target_words=700, min_story_words=300)   # one story only: RBI is dropped
    events, debates, impacts, srcs, links = material()

    class FrameModel(ScriptModel):
        def chat_json(self, task, messages, schema):
            user = messages[-1]["content"]
            if "Write three short parts" in user:
                return schema.model_validate({"lines": [
                    {"segment": "opening", "speaker": "A", "kind": "fact",
                     "text": "The Reserve Bank of India cut its repo rate to 5.25% on Friday.", "cites": ["F1"]},
                    {"segment": "opening", "speaker": "B", "kind": "transition",
                     "text": "The Reserve Bank of India cut its repo rate on Friday."}]}), "fake-writer"
            return super().chat_json(task, messages, schema)
    ep = ScriptService(s, FrameModel()).build("r", NOW, events, debates, impacts, srcs, links)
    assert [st.event_id for st in ep.plan.stories] == ["oil"]
    assert not any("Reserve Bank" in x.text for x in ep.lines if x.segment == "opening")


def test_revision_accepts_string_cites_and_first_answer_only():
    from intelpod.script.service import Revision
    r = Revision.model_validate({"lines": [{"id": "L1", "text": "a", "cites": "F1"}, {"id": None, "text": None}, "x"]})
    assert r.lines[0].cites == ["F1"] and r.lines[1].id == ""


def test_story_whose_model_lines_all_fail_falls_back_to_template():
    class BadModel(ScriptModel):
        def chat_json(self, task, messages, schema):
            user = messages[-1]["content"]
            if "failed fact checks" in user:
                return schema.model_validate({"lines": []}), "fake-writer"
            if "Segment" in user:
                return schema.model_validate({"lines": [{"speaker": "A", "kind": "fact",
                                                         "text": "Zorgon Corp said profits tripled.", "cites": ["F1"]}]}), "fake-writer"
            return super().chat_json(task, messages, schema)
    ep = build(BadModel())
    assert {x.event_id for x in ep.lines if x.segment == "story"} == {s.event_id for s in ep.plan.stories}
    assert ep.status == "partial" and any("template segment" in lim for lim in ep.limitations)


def test_markdown_urls_cannot_break_out():
    from intelpod.script.report import _url
    assert _url("https://x.test/a) ![x](https://evil.test/t.png") == "https://x.test/a%29%20!%5Bx%5D%28https://evil.test/t.png"


def test_episode_rebuilt_when_mode_or_evidence_changes_and_stale_files_removed(settings, tmp_path):
    from test_debate_pipeline import run_pipeline
    from intelpod.pipeline.runner import RunContext
    from intelpod.script.runner import run_script
    from intelpod.script.store import EpisodeStore
    settings.llm.enabled = False
    pipe, run_id = run_pipeline(settings, tmp_path)
    row = pipe.repo.get_run(run_id)
    ctx = RunContext(run_id=run_id, now=NOW, params=row.params, outputs={})
    assert run_script(pipe, ctx).get("reused")                   # same material and mode: reused
    settings.script.target_words = 900                          # settings changed: rebuilt
    assert not run_script(pipe, ctx).get("reused")
    # A refresh that produces no script removes the old files.
    d = settings.data_path / "runs" / run_id
    assert (d / "script.md").exists()
    settings.script.max_stories = 1
    from intelpod.debate.store import DebateStore
    from intelpod.db import DebatePackageRow
    with pipe.db.session() as s:
        s.query(DebatePackageRow).delete()
    run_script(pipe, ctx, refresh=True)
    assert EpisodeStore(pipe.db).episode(run_id).status == "skipped"
    pipe.stage_report(RunContext(run_id=run_id, now=row.params and NOW, params=row.params,
                                 outputs={k: v.output or {} for k, v in pipe.repo.stages(run_id).items()}))
    assert not (d / "script.md").exists()
