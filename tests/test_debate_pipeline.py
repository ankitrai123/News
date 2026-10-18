"""Phase 3 service and pipeline: viewpoint searches, model validation, honest limitations, stage
wiring, report, dashboard, API and CLI."""
import json

import respx
from fastapi.testclient import TestClient

from fixtures import synthetic_news as sn
from intelpod.db import Database
from intelpod.debate.registry import Registry
from intelpod.debate.service import DebateService
from intelpod.debate.store import DebateStore
from intelpod.pipeline.runner import Phase1Pipeline
from intelpod.web.app import create_app
from test_debate_units import NOW, brief, res
from test_research_pipeline import mock_research, setup
from test_pipeline import mock_sources


class FakeResearchStore:
    def __init__(self, results):
        self._results = results

    def package(self, run_id, event_id):
        return None

    def results(self, run_id, event_id, selected_only=False):
        return list(self._results)


class FakeResearch:
    providers = {"archive": None, "gdelt": None}

    def __init__(self, found=None):
        self.found = found or {}
        self.calls = []

    def supplement(self, run_id, brief, articles, queries, now, existing, reason, limit=3):
        self.calls.append((reason, [q.text for q in queries]))
        for key, results in self.found.items():
            if key in reason:
                return results[:limit]
        return []


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def chat_json(self, task, messages, schema):
        assert task == "debate_analysis"
        self.calls += 1
        if not self.answers:
            raise RuntimeError("no more answers")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return schema.model_validate(a), "fake-model"


def sources():
    return [res("a", "Reuters", "RBI cuts repo rate to 5.25% to support growth",
                extracts=["The Reserve Bank of India cut its repo rate to 5.25% to support growth, the governor said."],
                country="GB"),
            res("b", "CNBC", "Economists warn RBI rate cut risks inflation",
                extracts=["Economists warned the cut could push inflation higher."], content_type="analysis",
                country="US"),
            res("c", "BBC", "Reserve Bank of India lowers repo rate to 5.25%", country="GB")]


def service(tmp_path, settings, srcs, llm=None, research=None):
    store = DebateStore(Database(tmp_path / "d.db"))
    return DebateService(settings, FakeResearchStore(srcs), store, Registry([]), None, research_service=research,
                         llm_client=llm), store


def test_viewpoint_search_adds_sources_then_model_reruns(settings, tmp_path, taxonomy):
    added = res("d", "The Hindu", "Indian borrowers welcome RBI repo rate cut to 5.25%",
                extracts=["Home buyers said lower EMIs after the cut to 5.25% would help households."], country="IN")
    debate = {"questions": [{"question": "Will the cut support growth or push inflation higher?",
                             "disagreement_type": "predictive", "positions": [
                                 {"label": "Supports growth", "summary": "The cut to 5.25% aims to support growth.",
                                  "source_ids": ["s1", "s3"], "challenged_by": ["s2"]},
                                 {"label": "Inflation risk", "summary": "Economists warn the cut could push "
                                                                        "inflation higher.", "source_ids": ["s2"]}]}],
              "uncertainties": ["Whether inflation rises after the cut"],
              "would_change": ["Inflation data from the Reserve Bank of Japan"],
              "missing_perspectives": [{"perspective": "Indian borrowers", "keywords": ["borrowers", "home loans"]}]}
    llm = FakeLLM([debate, debate])
    research = FakeResearch({"India": [added]})
    svc, store = service(tmp_path, settings, sources(), llm, research)
    svc.taxonomy = taxonomy
    pkg = svc.debate_event("r1", brief(), [], NOW)
    assert llm.calls == 2                                         # before and after the viewpoint search
    assert pkg.status == "completed" and pkg.analysed_by == "fake-model"
    assert "d" in pkg.source_ids
    found = [v for v in pkg.viewpoint_searches if v.found]
    assert found and found[0].added_source_ids == ["d"]
    assert any(not v.found for v in pkg.viewpoint_searches)        # looked for, not found ...
    assert any(lim.startswith("Looked for") for lim in pkg.limitations)  # ... and said so
    q = pkg.questions[0]
    assert [p.source_ids for p in q.positions] == [["a", "c"], ["b"]]
    assert "Whether inflation rises after the cut" in pkg.uncertainties
    assert pkg.would_change == [] and any("Japan" in r for r in pkg.rejected)  # invented name refused
    assert store.package("r1", "e1").event_id == "e1"
    assert pkg.claims and all(c.text for c in pkg.claims)


def test_no_sources_means_no_debate(settings, tmp_path):
    svc, _ = service(tmp_path, settings, [], FakeLLM([]))
    pkg = svc.debate_event("r1", brief(), [], NOW)
    assert pkg.status == "skipped" and not pkg.claims and not pkg.questions
    assert "No retrieved sources" in pkg.limitations[0]


def test_without_model_claim_map_is_kept_and_limitation_stated(settings, tmp_path):
    svc, _ = service(tmp_path, settings, sources(), llm=None)
    pkg = svc.debate_event("r1", brief(), [], NOW)
    assert pkg.status == "partial" and pkg.claims and not pkg.questions and pkg.analysed_by is None
    assert any("No model available" in lim for lim in pkg.limitations)


def test_model_finding_no_disagreement_is_respected(settings, tmp_path):
    svc, _ = service(tmp_path, settings, sources(), FakeLLM([{"questions": []}]))
    pkg = svc.debate_event("r1", brief(), [], NOW)
    assert not pkg.questions and "does not manufacture" in pkg.no_disagreement_note


def test_fatal_model_error_stops_further_calls(settings, tmp_path):
    err = RuntimeError("bad key")
    err.fatal = True
    llm = FakeLLM([err, {"questions": []}])
    svc, _ = service(tmp_path, settings, sources(), llm)
    p1 = svc.debate_event("r1", brief(), [], NOW)
    p2 = svc.debate_event("r1", brief(event_id="e2"), [], NOW)
    assert llm.calls == 1 and p1.status == "partial" and p2.status == "partial"
    assert any("model analysis failed" in lim for lim in p1.limitations)


def test_model_budget_is_respected(settings, tmp_path):
    settings.debate.max_model_calls = 1
    llm = FakeLLM([{"questions": []}, {"questions": []}])
    svc, _ = service(tmp_path, settings, sources(), llm)
    svc.debate_event("r1", brief(), [], NOW)
    p2 = svc.debate_event("r1", brief(event_id="e2"), [], NOW)
    assert llm.calls == 1 and any("budget" in lim for lim in p2.limitations)


# -- pipeline -------------------------------------------------------------------------------
def run_pipeline(settings, tmp_path):
    setup(settings, tmp_path)
    with respx.mock(assert_all_called=False) as router:
        mock_sources(router)
        mock_research(router, [])
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
    return pipe, run_id


def test_debate_stage_runs_in_pipeline_and_reports(settings, tmp_path):
    settings.llm.enabled = False
    pipe, run_id = run_pipeline(settings, tmp_path)
    stages = pipe.repo.stages(run_id)
    assert list(stages) == ["ingest", "cluster", "rank", "review", "research", "debate", "impact", "script", "report"]
    out = stages["debate"].output
    assert out["status"] == "completed" and out["registry_sources"] >= 20
    pkgs = DebateStore(pipe.db).packages(run_id)
    assert pkgs and all(p.limitations for p in pkgs.values())
    assert all(p.status in ("partial", "skipped") for p in pkgs.values())  # no model: never "completed"
    run_dir = settings.data_path / "runs" / run_id
    assert "Viewpoints and evidence" in (run_dir / "phase1_events.md").read_text() or \
        all(p.status == "skipped" for p in pkgs.values())
    dj = json.loads((run_dir / "debate.json").read_text())
    assert set(dj) == set(pkgs)

    # Dashboard, API.
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    eid = next(iter(pkgs))
    page = client.get(f"/events/{eid}?run={run_id}")
    assert page.status_code == 200 and 'id="debate"' in page.text
    assert client.get("/").status_code == 200
    api = client.get(f"/api/events/{eid}/debate", params={"run": run_id}).json()
    assert api["event_id"] == eid and "claims" in api
    assert eid in client.get(f"/api/runs/{run_id}/debate").json()
    assert client.get("/api/events/nope/debate").status_code == 404


def test_debate_resume_and_cli(settings, tmp_path, monkeypatch, capsys):
    settings.llm.enabled = False
    pipe, run_id = run_pipeline(settings, tmp_path)
    store = DebateStore(pipe.db)
    before = {e: p.created_at for e, p in store.packages(run_id).items()}
    from intelpod.debate.runner import run_debate
    from intelpod.pipeline.runner import RunContext
    row = pipe.repo.get_run(run_id)
    stages = pipe.repo.stages(run_id)
    ctx = RunContext(run_id=run_id, now=sn.NOW, params=row.params,
                     outputs={k: v.output or {} for k, v in stages.items()})
    with respx.mock(assert_all_called=False) as router:
        router.route().respond(500)
        out = run_debate(pipe, ctx)
    assert all(e["status"] == "already_done" for e in out["per_event"])
    assert {e: p.created_at for e, p in store.packages(run_id).items()} == before

    from intelpod import cli
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    assert cli.main(["sources"]) == 0
    assert "Orientation is shown only where documented" in capsys.readouterr().out
    assert cli.main(["sources", "--gaps"]) == 0
    with respx.mock(assert_all_called=False) as router:
        router.route().respond(500)
        assert cli.main(["debate", "--run-id", run_id, "--refresh"]) == 0
    assert f"run: {run_id}" in capsys.readouterr().out
    assert cli.main(["debate", "--run-id", "bad id!"]) == 1


def test_bad_registry_or_debate_crash_does_not_fail_the_run(settings, tmp_path, monkeypatch):
    settings.llm.enabled = False
    bad = tmp_path / "sources.yaml"
    bad.write_text("sources:\n  - {publisher: BBC, domains: [bbc.com]\n")
    settings.debate.sources_file = str(bad)
    import intelpod.debate.runner as dr
    monkeypatch.setattr(dr, "DebateService", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipe, run_id = run_pipeline(settings, tmp_path)
    assert pipe.repo.get_run(run_id).status == "completed"
    out = pipe.repo.stages(run_id)["debate"].output
    assert out["status"] == "failed" and "boom" in out["reason"]
    md = (settings.data_path / "runs" / run_id / "phase1_events.md").read_text()
    assert "Viewpoint analysis (Phase 3) failed" in md


def test_research_refresh_clears_stale_debate(settings, tmp_path):
    settings.llm.enabled = False
    pipe, run_id = run_pipeline(settings, tmp_path)
    store = DebateStore(pipe.db)
    eid = next(iter(store.packages(run_id)))
    from intelpod.research.store import ResearchStore
    rs = ResearchStore(pipe.db)
    rs.save_event_research(rs.package(run_id, eid), [], [])
    assert store.package(run_id, eid) is None and eid not in store.done_event_ids(run_id)


def test_viewpoint_search_skips_copies_of_existing_sources(settings, tmp_path, taxonomy):
    from intelpod.research.models import SearchHit, SearchResponse
    from intelpod.research.service import ResearchService

    class Prov:
        def search(self, q):
            return SearchResponse(provider="archive", query_id=q.query_id, status="ok", hits=[
                SearchHit(provider="archive", query_id=q.query_id, query_type=q.query_type,
                          url="https://et.test/rbi", title="RBI cuts repo rate to 5.25% to support growth",
                          publisher="The Economic Times", domain="et.test", published_at=NOW)])

        def note(self):
            return None

    class Store:
        added = None

        def add_results(self, *a):
            Store.added = a

    existing = [res("a", "Reuters", "RBI cuts repo rate to 5.25% to support growth")]
    svc = ResearchService(settings, Store(), taxonomy, {"archive": Prov()})
    from intelpod.research.models import ResearchQuery
    q = ResearchQuery(query_id="q", event_id="e1", query_type="criticism", provider="archive", text="rbi",
                      terms=["rbi", "repo"], start=NOW, end=NOW)
    assert svc.supplement("r1", brief(), [], [q], NOW, existing, "test") == []
