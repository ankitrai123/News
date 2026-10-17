"""End-to-end Phase 1 pipeline with HTTP mocked by respx (synthetic data, no network)."""
from datetime import timedelta

import pytest
import respx

from fixtures import synthetic_news as sn
from intelpod.db import Database
from intelpod.pipeline import runner as runner_mod
from intelpod.pipeline.runner import Phase1Pipeline, StageError
from intelpod.providers.gdelt import GDELT_DOC_URL


def mock_sources(router, now=sn.NOW, fail=False):
    for key, (_, _, url, _) in sn.FEEDS.items():
        router.get(url).respond(503 if fail else 200, text="" if fail else sn.rss_xml(key, now))
    router.get(GDELT_DOC_URL).respond(503 if fail else 200, text="" if fail else sn.gdelt_json(now))


def test_end_to_end_run(settings):
    with respx.mock as router:
        mock_sources(router)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
    repo = pipe.repo
    assert repo.get_run(run_id).status == "completed"
    stages = repo.stages(run_id)
    assert [s for s in stages] == ["ingest", "cluster", "rank", "review", "report"]
    assert stages["review"].output["status"] == "skipped" and "NVIDIA_API_KEY" in stages["review"].output["reason"]
    ingest = stages["ingest"].output
    assert ingest["dedup"]["duplicate_url"] >= 1 and ingest["dedup"]["duplicate_title_same_publisher"] >= 1
    assert ingest["providers"]["rss"]["ok"] == 3 and ingest["providers"]["gdelt"]["ok"] == 1
    selected = repo.scores_for_run(run_id, selected_only=True)
    assert 3 <= len(selected) <= 8
    chip = next(r for r in selected if "semiconductor" in r.title.lower() or "chip" in r.title.lower())
    assert chip.article_count == 5  # 4 RSS + 1 GDELT phrasing
    md = (settings.data_path / "runs" / run_id / "phase1_events.md").read_text()
    assert "Why selected" in md and "https://" in md
    assert (settings.data_path / "runs" / run_id / "phase1_events.json").exists()
    assert Database(settings.db_file).integrity_check() == "ok"


def test_second_run_links_continuing_events_and_skips_known_articles(settings):
    with respx.mock as router:
        mock_sources(router)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run1 = pipe.run(now=sn.NOW)
        run2 = pipe.run(now=sn.NOW + timedelta(hours=2))
    s2 = pipe.repo.stages(run2)["ingest"].output
    assert s2["inserted"] == 0 and s2["dedup"]["duplicate_url"] > 0
    ids1 = {r.event_id for r in pipe.repo.scores_for_run(run1)}
    r2 = pipe.repo.scores_for_run(run2)
    assert {r.event_id for r in r2} == ids1
    assert all(r.is_continuing for r in r2)
    nov = next(c for c in r2[0].components if c.name == "novelty")
    assert nov.value == pytest.approx(0.3)


def test_all_sources_down_fails_clearly(settings):
    with respx.mock as router:
        mock_sources(router, fail=True)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        with pytest.raises(StageError, match="every provider request failed"):
            pipe.run(now=sn.NOW)
    run = pipe.repo.latest_run(status=None)
    assert run.status == "failed" and "ingest" in run.error
    assert len([f for f in pipe.repo.fetch_log() if not f.ok]) == 4


def test_resume_after_interrupted_stage_does_not_refetch(settings, monkeypatch):
    with respx.mock as router:
        mock_sources(router)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        original = pipe.stage_fns["rank"]
        pipe.stage_fns["rank"] = lambda ctx: (_ for _ in ()).throw(RuntimeError("simulated crash"))
        with pytest.raises(RuntimeError):
            pipe.run(now=sn.NOW)
        run = pipe.repo.latest_run(status=None)
        assert run.status == "failed"
        assert pipe.repo.stages(run.run_id)["rank"].attempts == 2  # retried once
        calls_before = len(router.calls)
        pipe.stage_fns["rank"] = original
        pipe.run(run_id=run.run_id, resume=True)
        assert len(router.calls) == calls_before  # ingest + cluster reused, no new HTTP
    assert pipe.repo.get_run(run.run_id).status == "completed"
    assert pipe.repo.stages(run.run_id)["ingest"].attempts == 1


def test_stop_after_then_resume(settings):
    with respx.mock as router:
        mock_sources(router)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW, stop_after="ingest")
        assert pipe.repo.get_run(run_id).status == "paused"
        pipe.run(run_id=run_id, resume=True)
    assert pipe.repo.get_run(run_id).status == "completed"


def test_provider_crash_does_not_sink_run(settings):
    class Broken:
        name = "broken"
        def fetch(self):
            raise ValueError("bad payload")
    with respx.mock as router:
        mock_sources(router)
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        from intelpod.http import HttpClient
        http = HttpClient(settings.http, "t")
        pipe._providers = pipe.build_providers(http) + [Broken()]
        run_id = pipe.run(now=sn.NOW)
    out = pipe.repo.stages(run_id)["ingest"].output
    assert out["providers"]["broken"]["ok"] == 0 and pipe.repo.get_run(run_id).status == "completed"


def test_concurrent_run_rejected(settings):
    pipe = Phase1Pipeline(settings)
    pipe.repo.create_run("run_x", "phase1", {"now": sn.NOW.isoformat()})
    with pytest.raises(runner_mod.RunInProgress):
        pipe.run(now=sn.NOW)
