import respx
from fastapi.testclient import TestClient

from fixtures import synthetic_news as sn
from intelpod.pipeline.runner import Phase1Pipeline
from intelpod.web.app import create_app
from test_pipeline import mock_sources


def test_dashboard_and_api(settings):
    with respx.mock as router:
        mock_sources(router)
        run_id = Phase1Pipeline(settings, stage_retry_delay=0).run(now=sn.NOW)
    client = TestClient(create_app(settings))
    assert client.get("/api/health").json()["status"] == "ok"
    latest = client.get("/api/events/latest").json()
    assert latest["run_id"] == run_id and latest["events"]
    eid = latest["events"][0]["event_id"]
    ev = client.get(f"/api/events/{eid}").json()
    assert ev["articles"] and ev["score_history"]
    assert client.get(f"/api/runs/{run_id}").json()["stages"]["rank"]["status"] == "completed"
    assert client.get("/api/events/does-not-exist").status_code == 404
    for path in ["/", f"/events/{eid}", "/runs", "/feeds", "/settings"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert latest["events"][0]["title"] in client.get("/").text
    assert len(client.get("/api/feeds").json()) == 3


def test_trigger_run_endpoint_uses_background_thread(settings):
    calls = []
    class FakePipe:
        def run(self, **kw):
            calls.append(kw)
    client = TestClient(create_app(settings, pipeline_factory=FakePipe))
    assert client.post("/api/runs").status_code == 202
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls == [{"resume": False, "run_id": None}]
