"""Phase 4 end to end: impact stage in a full run with a scripted model; brief files, dashboard,
API, CLI, forecast ledger, and "what's new" on a second run."""
import json
import re

import respx
from fastapi.testclient import TestClient

from fixtures import synthetic_news as sn
from intelpod.impact.store import ImpactStore
from intelpod.pipeline.runner import Phase1Pipeline
from intelpod.web.app import create_app
from test_research_pipeline import mock_research, setup
from test_pipeline import mock_sources


class ScriptedModel:
    """Answers like a model would, using only what it was sent (so IntelPod's checks pass),
    plus one invented chain that the checks must reject."""

    provider = "fake"

    def __init__(self):
        self.tasks = []

    def availability(self):
        return True, "ok"

    def chat_json(self, task, messages, schema):
        self.tasks.append(task)
        user = messages[-1]["content"]
        if task == "impact_analysis" and "links" in schema.model_fields:
            m = re.search(r"quotes: \[[^\]]+\] ([^|\n]+)", user)
            return schema.model_validate({"links": [{"link": "L1", "explanation": m.group(1).strip() if m else ""}]}), "fake-model"
        if task == "impact_analysis":
            first = re.search(r"^c1 \[[^\]]*\] (.+)$", user, re.M).group(1)
            return schema.model_validate({
                "what_happened": first,
                "chains": [
                    {"title": "Costs for companies", "horizon": "short_term", "direction": "negative",
                     "dimensions": ["corporate"], "sectors": ["manufacturing"], "india": False,
                     "steps": [{"text": first, "claim_ids": ["c1"]},
                               {"text": "Higher costs could follow for companies that depend on it."}]},
                    {"title": "Invented", "steps": [{"text": first, "claim_ids": ["c1"]},
                                                    {"text": "Goldman Sachs sees a 0.7% hit to growth."}]}],
                "actors": [{"group": "Importers", "effect": "harmed", "reason": "Costs may rise."}],
                "interpretations": [{"stance": "negative", "summary": first, "claim_ids": ["c1"]}],
                "india_summary": ""}), "fake-model"
        raise RuntimeError(f"not scripted: {task}")


def run(settings, tmp_path, model=None, now=sn.NOW):
    setup(settings, tmp_path)
    with respx.mock(assert_all_called=False) as router:
        mock_sources(router)
        mock_research(router, [])
        pipe = Phase1Pipeline(settings, stage_retry_delay=0,
                              llm_client_factory=(lambda run_id: model) if model else None)
        run_id = pipe.run(now=now)
    return pipe, run_id


def test_impact_stage_with_model_builds_checked_brief(settings, tmp_path):
    model = ScriptedModel()
    pipe, run_id = run(settings, tmp_path, model)
    stages = pipe.repo.stages(run_id)
    assert list(stages) == ["ingest", "cluster", "rank", "review", "research", "debate", "impact", "script", "report"]
    out = stages["impact"].output
    assert out["status"] == "completed" and out["new_forecasts"] >= 1
    store = ImpactStore(pipe.db)
    analyses = store.analyses(run_id)
    a = next(x for x in analyses.values() if x.chains)
    assert a.status == "completed" and a.analysed_by == "fake-model"
    assert [c.title for c in a.chains] == ["Costs for companies"]           # invented chain rejected
    assert any("0.7%" in r or "Goldman" in r for r in a.rejected)
    ch = a.chains[0]
    assert ch.steps[0].basis == "reported" and ch.steps[1].basis == "inferred" and ch.status == "conditional"
    assert [x.group for x in a.actors] == ["Importers"] and a.actors[0].basis == "inferred"
    assert "does not add an opposing one" in a.interpretation_note

    brief = store.brief(run_id)
    assert brief is not None
    # In this fixture the only cross-event "evidence" is one event's own article reused in another
    # event's research: that is not a source referring to the other event, so no link is evidenced.
    assert all(link.kind != "evidenced" for link in brief.links)
    run_dir = settings.data_path / "runs" / run_id
    md = (run_dir / "intelligence_brief.md").read_text()
    assert "Effect chain — Costs for companies" in md
    assert "inference" in md and "Goldman" not in md
    js = json.loads((run_dir / "intelligence_brief.json").read_text())
    assert js["events"] and js["links"] and "growth" in js["dimensions"]

    # Dashboard and API.
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    page = client.get(f"/events/{a.event_id}?run={run_id}")
    assert page.status_code == 200 and 'id="impact"' in page.text and "Costs for companies" in page.text
    home = client.get(f"/?run={run_id}")
    assert "Impact:" in home.text
    assert client.get(f"/api/events/{a.event_id}/impact", params={"run": run_id}).json()["event_id"] == a.event_id
    assert client.get(f"/api/runs/{run_id}/brief").json()["brief"]["run_id"] == run_id
    fc = client.get("/api/forecasts").json()
    assert fc and all(f["status"] == "open" for f in fc)
    page = client.get("/forecasts")
    assert page.status_code == 200 and "Forecast ledger" in page.text
    fid = fc[0]["forecast_id"]
    r = client.post(f"/forecasts/{fid}/resolve", data={"verdict": "partly", "outcome": "Half of it happened."},
                    follow_redirects=False)
    assert r.status_code == 303
    again = client.post(f"/forecasts/{fid}/resolve", data={"verdict": "correct", "outcome": "Overwrite attempt"})
    assert again.status_code == 409
    assert client.get("/api/forecasts", params={"status": "resolved"}).json()[0]["verdict"] == "partly"
    assert client.post("/forecasts/nope/resolve", data={"verdict": "correct", "outcome": "xyz"}).status_code == 404
    assert client.post(f"/forecasts/{fid}/resolve", data={"verdict": "maybe", "outcome": "xyz"}).status_code == 422


def test_without_model_evidence_tables_and_honest_limits(settings, tmp_path):
    settings.llm.enabled = False
    pipe, run_id = run(settings, tmp_path)
    analyses = ImpactStore(pipe.db).analyses(run_id)
    assert analyses and all(a.status in ("partial", "skipped") for a in analyses.values())
    a = next(x for x in analyses.values() if x.dimensions)
    assert not a.chains and any("not assessed" in lim for lim in a.limitations)
    assert all(f.kind in ("measured", "forecast", "stated") for x in analyses.values() for f in x.figures)


def test_second_run_reports_whats_new_and_cli(settings, tmp_path, monkeypatch, capsys):
    from datetime import timedelta
    settings.llm.enabled = False
    pipe, first = run(settings, tmp_path)
    pipe2, second = run(settings, tmp_path, now=sn.NOW + timedelta(hours=6))
    store = ImpactStore(pipe2.db)
    later = store.analyses(second)
    assert any(a.context.previous_run_id == first for a in later.values())

    from intelpod import cli
    monkeypatch.setattr(cli, "_settings", lambda args: settings)
    assert cli.main(["impact", "--run-id", second, "--refresh"]) == 0
    out = capsys.readouterr().out
    assert f"run: {second}" in out and "brief report:" in out
    assert cli.main(["forecasts", "--status", "open"]) == 0
    listed = capsys.readouterr().out
    fid = re.search(r"^([0-9a-f]{16})", listed, re.M).group(1)
    assert cli.main(["forecasts", "resolve", fid, "--verdict", "unresolvable", "--outcome", "No data yet."]) == 0
    assert cli.main(["forecasts", "resolve", fid]) == 1
    assert cli.main(["forecasts", "resolve", fid, "--verdict", "correct", "--outcome", "again"]) == 1  # protected
    assert cli.main(["forecasts", "resolve", fid, "--verdict", "correct", "--outcome", "Data out.", "--amend"]) == 0

    # Refreshing a run keeps its new forecasts in the brief; an old run is never compared with a later one.
    brief_before = store.brief(second).new_forecasts
    assert cli.main(["impact", "--run-id", second, "--refresh"]) == 0
    assert store.brief(second).new_forecasts == brief_before
    assert cli.main(["impact", "--run-id", first, "--refresh"]) == 0
    assert all(a.context.previous_run_id != second for a in store.analyses(first).values())
    assert cli.main(["impact", "--run-id", "bad id!"]) == 1
