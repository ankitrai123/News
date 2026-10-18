"""Phase 2 end to end: the research stage inside a full run (synthetic data, HTTP mocked)."""
import json
import re

import httpx
import respx

from fixtures import synthetic_news as sn
from intelpod.db import ResearchResultRow
from intelpod.pipeline.runner import Phase1Pipeline
from intelpod.research.store import ResearchStore
from test_pipeline import mock_sources

PAGE = """<html><head><title>{title}</title>
<script type="application/ld+json">{{"@type":"AnalysisNewsArticle","datePublished":"{date}",
"author":{{"name":"A. Writer"}}}}</script></head><body><article>
<p>The United States announced new semiconductor export controls on China on Thursday, officials said.</p>
<p>Analysts said the chip export rules would hit Chinese firms and global suppliers alike.</p>
<p>{long}</p></article></body></html>"""

RESEARCH_FEED = """<?xml version='1.0'?><rss version='2.0'><channel><title>Think tank</title>
<item><title>Analysis: what new chip export controls on China mean for suppliers</title>
<link>https://thinktank.test/research/chip-controls</link><pubDate>{date}</pubDate>
<description>Our analysts assess the semiconductor export controls on China.</description></item>
</channel></rss>"""


def setup(settings, tmp_path):
    feeds = tmp_path / "research_feeds.yaml"
    feeds.write_text("feeds:\n  - {name: TT, publisher: Example Think Tank, domain: thinktank.test, "
                     "url: 'https://thinktank.test/feed', country: US, source_type: think_tank, "
                     "enabled: true, verified: false}\n")
    settings.research.feeds.feeds_file = str(feeds)
    settings.research.min_results = 2


def mock_research(router, page_calls):
    date = sn.NOW.strftime("%a, %d %b %Y %H:%M:%S +0000")
    router.get("https://thinktank.test/feed").respond(200, text=RESEARCH_FEED.format(date=date))

    def page(request):
        page_calls.append(str(request.url))
        if "gdelt-sample" in request.url.host:
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text=PAGE.format(title="Chip export rules", date="2026-10-16T09:00:00Z",
                                                   long="Full article body sentence that must never be stored. " * 60))
        return httpx.Response(404)

    router.route(host__regex=r".*\.test$", path__regex=r"^(?!/feed$|/rss).*").mock(side_effect=page)


def test_research_stage_builds_packages_and_stores_no_full_text(settings, tmp_path):
    setup(settings, tmp_path)
    page_calls: list[str] = []
    with respx.mock(assert_all_called=False) as router:
        mock_sources(router)
        mock_research(router, page_calls)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
    stages = pipe.repo.stages(run_id)
    out = stages["research"].output
    assert stages["research"].status == "completed" and out["status"] == "completed"
    assert set(out["providers"]) >= {"archive", "feeds", "gdelt"} and out["research_feeds"]["new_items"] == 1
    assert out["attribution"].startswith("Discovery via The GDELT Project")

    store = ResearchStore(pipe.db)
    selected = pipe.repo.scores_for_run(run_id, selected_only=True)
    pkgs = store.packages(run_id)
    assert set(pkgs) == {r.event_id for r in selected}
    chip = next(r for r in selected if "chip" in r.title.lower() or "semiconductor" in r.title.lower())
    pkg = pkgs[chip.event_id]
    results = store.results(run_id, chip.event_id)
    chosen = [r for r in results if r.selected]
    assert chosen and set(pkg.result_ids) == {r.result_id for r in chosen}
    assert all(r.selection_reason for r in results)
    tt = next(r for r in chosen if r.publisher == "Example Think Tank")
    assert tt.content_type in ("research", "analysis") and "feeds" in tt.providers
    gd = next(r for r in results if "gdelt-sample" in r.url)
    assert gd.page_status == "fetched" and gd.date_source == "page_metadata" and gd.evidence_notes
    assert all(len(n.text) <= 300 for n in gd.evidence_notes)
    assert pkg.limitations  # e.g. no primary source / few sources: always stated
    queries = store.queries(run_id, chip.event_id)
    assert any(q.provider == "gdelt" for q in queries) and any(q.provider == "archive" for q in queries)

    # Copyright: no stored row carries the article body.
    with pipe.db.session() as s:
        for row in s.query(ResearchResultRow).all():
            assert "must never be stored" not in json.dumps(row.payload)
    report = (settings.data_path / "runs" / run_id / "phase1_events.md").read_text()
    assert "**Research (" in report and "Discovery via The GDELT Project" in report
    rj = json.loads((settings.data_path / "runs" / run_id / "research.json").read_text())
    assert chip.event_id in rj and "must never be stored" not in json.dumps(rj)

    # GDELT research requests are logged for quota tracking, with dates in the request.
    logs = [f for f in pipe.repo.fetch_log(run_id) if f.provider == "gdelt_research"]
    assert logs and all("query=" in f.target for f in logs)


def test_research_resume_does_not_repeat_finished_events(settings, tmp_path):
    setup(settings, tmp_path)
    page_calls: list[str] = []
    with respx.mock(assert_all_called=False) as router:
        mock_sources(router)
        mock_research(router, page_calls)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        real = pipe.stage_fns["report"]
        pipe.stage_fns["report"] = lambda ctx: (_ for _ in ()).throw(RuntimeError("crash in report"))
        try:
            pipe.run(now=sn.NOW)
        except RuntimeError:
            pass
        run = pipe.repo.latest_run(status=None)
        assert run.status == "failed" and pipe.repo.stages(run.run_id)["research"].status == "completed"
        gdelt_before = sum(1 for c in router.calls if "gdeltproject" in str(c.request.url))
        pipe.stage_fns["report"] = real
        pipe.run(resume=True, run_id=run.run_id)
        gdelt_after = sum(1 for c in router.calls if "gdeltproject" in str(c.request.url))
    assert gdelt_after == gdelt_before  # research output reused, nothing re-queried
    assert pipe.repo.get_run(run.run_id).status == "completed"


def test_research_survives_every_provider_failing(settings, tmp_path):
    setup(settings, tmp_path)
    settings.ingestion.providers.gdelt.cooldown_seconds = 0
    with respx.mock(assert_all_called=False) as router:
        gdelt = router.route(host="api.gdeltproject.org").respond(429)  # first route wins: ingest and research
        router.get("https://thinktank.test/feed").respond(503)
        mock_sources(router)
        router.route(host__regex=r".*\.test$").respond(500)            # every result page fails
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
    assert gdelt.call_count >= 2
    assert pipe.repo.get_run(run_id).status == "completed"
    out = pipe.repo.stages(run_id)["research"].output
    pkgs = ResearchStore(pipe.db).packages(run_id)
    assert pkgs and all(p.limitations for p in pkgs.values())
    assert "gdelt" in json.dumps(out["provider_notes"]).lower()
    assert re.search(r"archive", json.dumps(out["providers"]))


def test_dashboard_and_api_show_research(settings, tmp_path):
    from fastapi.testclient import TestClient
    from intelpod.web.app import create_app
    setup(settings, tmp_path)
    with respx.mock(assert_all_called=False) as router:
        mock_sources(router)
        mock_research(router, [])
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
    client = TestClient(create_app(settings), base_url="http://127.0.0.1")
    chip = next(r for r in pipe.repo.scores_for_run(run_id, selected_only=True) if "chip" in r.title.lower()
                or "semiconductor" in r.title.lower())
    index = client.get(f"/?run={run_id}").text
    assert "Research:</b>" in index and f"/events/{chip.event_id}?run={run_id}#research" in index
    page = client.get(f"/events/{chip.event_id}?run={run_id}").text
    assert 'id="research"' in page and "Limitations" in page and "Searches run" in page
    assert "Example Think Tank" in page and "The GDELT Project" in page
    api = client.get(f"/api/events/{chip.event_id}/research?run={run_id}").json()
    assert api["package"]["event_id"] == chip.event_id and api["sources"]
    assert all(s["selected"] for s in api["sources"])
    assert client.get(f"/api/runs/{run_id}/research").json()[chip.event_id]["sources"] >= 1
    assert client.get("/api/events/nope/research").status_code == 404
    assert client.get(f"/api/runs/{run_id}/research").status_code == 200


def test_cli_research_command_reresearches_a_run(settings, tmp_path, monkeypatch, capsys):
    from intelpod import cli
    setup(settings, tmp_path)
    settings.research.enabled = False  # Phase 1 only first
    with respx.mock(assert_all_called=False) as router:
        mock_sources(router)
        mock_research(router, [])
        run_id = Phase1Pipeline(settings, stage_retry_delay=0).run(now=sn.NOW)
        monkeypatch.setattr(cli, "_settings", lambda args: settings)
        assert cli.main(["research", "--run-id", run_id]) == 0
    out = capsys.readouterr().out
    assert "sources (" in out and "markdown report" in out
    assert ResearchStore(Phase1Pipeline(settings).db).packages(run_id)


def test_model_keywords_and_grounded_notes_in_pipeline(settings, tmp_path):
    from intelpod.research.model_assist import KeywordIdeas, NotesResponse, SourceNote
    setup(settings, tmp_path)
    calls = []

    class FakeLLM:
        provider = "fake"

        def chat_json(self, task, messages, schema):
            calls.append(task)
            if task == "event_review":
                raise RuntimeError("review not exercised here")
            if schema is KeywordIdeas:
                return KeywordIdeas(core=["chip export controls"], criticism=["export curbs criticism"]), "m"
            text = messages[1]["content"]
            ids = re.findall(r"^(s\d+) \|", text, re.M)
            return NotesResponse(sources=[SourceNote(id=i, note="Invented claim by Zorblax Corp of 999 billion.")
                                          for i in ids[:1]] +
                                 [SourceNote(id=i, note="It reports new semiconductor export controls on China.")
                                  for i in ids[1:]]), "m"

    settings.llm.event_review.enabled = False
    with respx.mock(assert_all_called=False) as router:
        mock_sources(router)
        mock_research(router, [])
        pipe = Phase1Pipeline(settings, stage_retry_delay=0, llm_client_factory=lambda run_id: FakeLLM())
        run_id = pipe.run(now=sn.NOW)
    assert "research_queries" in calls and "research_notes" in calls
    store = ResearchStore(pipe.db)
    notes = [n.text for eid in store.packages(run_id) for r in store.results(run_id, eid, selected_only=True)
             for n in r.evidence_notes if n.kind == "model_note"]
    assert notes and not any("Zorblax" in n for n in notes)  # ungrounded note rejected
    queries = [q for eid in store.packages(run_id) for q in store.queries(run_id, eid)]
    assert any("chip export controls" in q.text for q in queries)
