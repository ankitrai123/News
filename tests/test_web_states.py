"""Dashboard behaviours reported in user testing (run controls, states, validation)."""
import respx
from fastapi.testclient import TestClient

from fixtures import synthetic_news as sn
from intelpod.db import Database
from intelpod.pipeline.runner import Phase1Pipeline
from intelpod.repository import Repository
from intelpod.web.app import create_app
from test_pipeline import mock_sources


class FakePipe:
    def __init__(self, calls):
        self.calls = calls

    def run(self, **kw):
        self.calls.append(kw)


def completed_run(settings):
    with respx.mock as router:
        mock_sources(router)
        return Phase1Pipeline(settings, stage_retry_delay=0).run(now=sn.NOW)


def app_with(settings, calls=None):
    calls = calls if calls is not None else []
    return TestClient(create_app(settings, pipeline_factory=lambda: FakePipe(calls)), base_url="http://127.0.0.1"), calls


def test_resume_when_latest_completed_says_nothing_to_resume(settings):
    completed_run(settings)
    client, calls = app_with(settings)
    r = client.post("/runs/start", data={"resume": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=nothing")
    page = client.get(r.headers["location"]).text
    assert "Nothing to resume" in page and calls == []
    assert client.post("/api/runs?resume=true").status_code == 409


def test_resume_form_resumes_failed_run(settings):
    repo = Repository(Database(settings.db_file))
    repo.create_run("run_failed_1", "phase1", {"now": sn.NOW.isoformat()})
    repo.set_run_status("run_failed_1", "failed", "ingest: boom")
    client, calls = app_with(settings)
    r = client.post("/runs/start", data={"resume": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=resumed")
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls == [{"resume": True, "run_id": "run_failed_1"}]


def test_start_new_run_from_form(settings):
    client, calls = app_with(settings)
    r = client.post("/runs/start", data={}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=started")


def test_unknown_run_and_event_are_html_404s(settings):
    completed_run(settings)
    client, _ = app_with(settings)
    r = client.get("/?run=bogus")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"] and "not found" in r.text
    assert "No runs yet" not in r.text and "No completed runs" not in r.text
    r = client.get("/events/bogus")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    assert client.get("/api/events/bogus").json()["detail"] == "event not found"
    assert client.get("/api/runs/bogus/events").status_code == 404


def test_api_limit_bounds(settings):
    client, _ = app_with(settings)
    assert client.get("/api/runs?limit=99999999999999999999").status_code == 422
    assert client.get("/api/runs?limit=-1").status_code == 422
    assert client.get("/api/runs?limit=0").status_code == 422
    assert client.get("/api/runs?limit=500").status_code == 200


def test_in_progress_run_is_not_shown_as_quiet_day(settings):
    repo = Repository(Database(settings.db_file))
    repo.create_run("run_live_1", "phase1", {"now": sn.NOW.isoformat()})
    repo.stage_start("run_live_1", "ingest")
    client, _ = app_with(settings)
    page = client.get("/?run=run_live_1").text
    assert "still in progress" in page and "current stage: ingest" in page
    assert "Quiet day" not in page and "quiet day" not in page and "? articles" not in page
    assert 'http-equiv="refresh"' in page  # auto-refresh while running
    runs = client.get("/runs").text
    import re
    assert re.search(r"<button[^>]*\bdisabled\b[^>]*>Start new run", runs)
    assert re.search(r"<button[^>]*\bdisabled\b[^>]*>Resume latest", runs) and "is in progress" in runs


def test_event_page_reports_match_the_run(settings):
    run_id = completed_run(settings)
    repo = Repository(Database(settings.db_file))
    sel = repo.scores_for_run(run_id, selected_only=True)[0]
    # A later run links one more (fake) article to the same event.
    from intelpod.pipeline.normalize import normalize_item
    from intelpod.schemas import RawItem
    extra = normalize_item(RawItem(provider="rss", title="Late follow-up report on the same story here",
                                   url="https://late.test/x", source_name="Late Pub", published_at=sn.NOW), now=sn.NOW)
    repo.save_articles([extra])
    from intelpod.db import EventArticleRow
    with repo.db.session() as s:
        s.add(EventArticleRow(event_id=sel.event_id, article_id=extra.article_id, first_run_id="run_later"))
    client, _ = app_with(settings)
    html = client.get(f"/events/{sel.event_id}?run={run_id}").text
    assert f"Reports ({sel.article_count})" in html
    assert "linked to it across all runs" in html


def test_labels_plurals_and_utc(settings):
    run_id = completed_run(settings)
    client, _ = app_with(settings)
    html = client.get(f"/?run={run_id}").text
    assert "1 reports" not in html and "some_dates_approximate" not in html and "gdelt_seendate" not in html
    assert " UTC" in html and 'aria-current="page"' in html and 'rel="icon"' in html
    assert 'role="img" aria-label=' in html


def test_settings_page_shows_key_status_and_placeholder_warning(settings):
    settings.app.user_agent = "IntelPod/0.1 (contact: set-your-email@example.com)"
    client, _ = app_with(settings)
    html = client.get("/settings").text
    assert "NVIDIA_API_KEY: not set" in html and "placeholder contact" in html and "Phase 1 needs none" not in html


def _orphan(repo, run_id):
    """A run left 'running' by a process that no longer exists."""
    from intelpod.db import PipelineRunRow
    repo.create_run(run_id, "phase1", {"now": sn.NOW.isoformat()})
    repo.stage_start(run_id, "ingest")
    with repo.db.session() as s:
        s.get(PipelineRunRow, run_id).owner = f"{__import__('socket').gethostname()}:999999999"


def test_interrupted_run_is_detected_and_resumable(settings):
    repo = Repository(Database(settings.db_file))
    _orphan(repo, "run_killed_1")
    client, calls = app_with(settings)
    assert client.get("/api/health").json()["run_in_progress"] is False
    row = repo.get_run("run_killed_1")
    assert row.status == "failed" and "interrupted" in row.error
    assert repo.stages("run_killed_1")["ingest"].status == "failed"
    html = client.get("/").text  # only runs are unfinished: show that run, not "No runs yet"
    assert "No runs yet" not in html and "run_killed_1" in html and "Resume it" in html
    r = client.post("/runs/start", data={"resume": "1", "run_id": "run_killed_1"}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=resumed")
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls == [{"resume": True, "run_id": "run_killed_1"}]


def test_resume_button_resumes_the_run_on_screen(settings):
    repo = Repository(Database(settings.db_file))
    for rid in ("run_a_1", "run_b_1"):
        repo.create_run(rid, "phase1", {"now": sn.NOW.isoformat()})
        repo.set_run_status(rid, "failed", "boom")
    client, calls = app_with(settings)
    assert 'name="run_id" value="run_a_1"' in client.get("/?run=run_a_1").text
    client.post("/runs/start", data={"resume": "1", "run_id": "run_a_1"}, follow_redirects=False)
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls == [{"resume": True, "run_id": "run_a_1"}]


def test_api_run_id_validation(settings):
    completed = completed_run(settings)
    client, calls = app_with(settings)
    assert client.post(f"/api/runs?run_id={completed}").status_code == 422      # run_id needs resume
    assert client.post("/api/runs?resume=true&run_id=nope").status_code == 404
    assert client.post(f"/api/runs?resume=true&run_id={completed}").status_code == 409
    assert client.post("/api/runs?resume=true&run_id=../../x").status_code == 422
    assert calls == []


def test_refresh_drops_one_off_message(settings):
    repo = Repository(Database(settings.db_file))
    repo.create_run("run_live_2", "phase1", {"now": sn.NOW.isoformat()})
    client, _ = app_with(settings)
    html = client.get("/runs?msg=started").text
    assert 'content="10;url=/runs"' in html
