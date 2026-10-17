"""End-to-end run with NewsData.io and NVIDIA (embeddings + chat) all MOCKED via respx.

Synthetic data only; verifies wiring, caching, review blending and graceful degradation.
"""
import hashlib
import json
import math

import httpx
import pytest
import respx

from fixtures import synthetic_news as sn
from intelpod.config import NewsdataQuery
from intelpod.pipeline.runner import Phase1Pipeline
from test_pipeline import mock_sources

NV = "https://integrate.api.nvidia.com/v1"
ND = "https://newsdata.io/api/1/latest"


def fake_embedding(text: str, dim: int = 48) -> list[float]:
    """Deterministic bag-of-words embedding with a large shared component (high cosine floor)."""
    v = [0.0] * dim
    for w in text.lower().split():
        h = int(hashlib.md5(w.strip(".,:;'").encode()).hexdigest(), 16)
        v[h % dim] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    v = [x / n for x in v]
    v[0] += 1.5
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def embeddings_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert body["input_type"] == "passage" and body["truncate"] == "END"
    return httpx.Response(200, json={"object": "list", "model": body["model"],
                                     "data": [{"index": i, "embedding": fake_embedding(t)}
                                              for i, t in enumerate(body["input"])]})


def chat_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    reports = [ln for ln in user.splitlines() if ln.startswith("r") and " | " in ln]
    first_headline = reports[0].split(" | ")[3].split(" — ")[0]
    is_oil = "OPEC" in user
    answer = {
        "same_event": True, "outlier_ids": [], "title": first_headline,
        "summary": "Invented: the Prime Minister of Atlantis resigned." if is_oil else "",
        "soft_news": False,
        "scores": {d: {"score": 0.9 if (d == "economic" and is_oil) else 0.4, "reason": "test"}
                   for d in ("geopolitical", "economic", "business", "india", "long_term")},
    }
    return httpx.Response(200, json={"model": body["model"], "choices": [
        {"index": 0, "message": {"role": "assistant", "content": json.dumps(answer)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 80}})


@pytest.fixture
def full_settings(settings, monkeypatch):
    monkeypatch.setenv("NEWSDATA_API_KEY", "nd-test-key")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    settings.ingestion.providers.newsdata.queries = [NewsdataQuery(topic="india", category="business", country="in")]
    settings.ingestion.providers.newsdata.min_interval_seconds = 0
    settings.clustering.embeddings.enabled = True
    settings.llm.providers["nvidia"].requests_per_minute = 0
    settings.llm.event_review.max_events = 5
    return settings


def mock_all(router):
    mock_sources(router)
    nd = router.get(ND).respond(200, json=sn.newsdata_json(), headers={"X-API-Limit-Remaining": "199"})
    emb = router.post(NV + "/embeddings").mock(side_effect=embeddings_handler)
    chat = router.post(NV + "/chat/completions").mock(side_effect=chat_handler)
    return nd, emb, chat


def test_full_run_with_newsdata_and_nvidia(full_settings):
    with respx.mock as router:
        nd, emb, chat = mock_all(router)
        pipe = Phase1Pipeline(full_settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
        stages = pipe.repo.stages(run_id)
        ingest = stages["ingest"].output
        assert ingest["providers"]["newsdata"]["ok"] == 1 and ingest["providers"]["newsdata"]["credits_remaining"] == 199
        assert "used (nvidia/" in stages["cluster"].output["embeddings"]
        review = stages["review"].output
        assert review["status"] == "completed" and review["reviewed"] == 5 and chat.call_count == 5
        assert review["summaries_rejected"] >= 1  # invented "Atlantis" summary was rejected

        ranked = pipe.repo.scores_for_run(run_id)
        oil = next(r for r in ranked if "opec" in r.title.lower())
        econ = next(c for c in oil.components if c.name == "economic")
        assert any("model 0.90" in e for e in econ.evidence)
        assert oil.summary is None and "summary" in oil.review.rejected
        report = (full_settings.data_path / "runs" / run_id / "phase1_events.md").read_text()
        assert "nemotron" in report

        # Second run: same articles -> embeddings and chat answers come from the caches.
        emb_calls, chat_calls = emb.call_count, chat.call_count
        run2 = pipe.run(now=sn.NOW)
        assert emb.call_count == emb_calls and chat.call_count == chat_calls
        assert "0 new" in pipe.repo.stages(run2)["cluster"].output["embeddings"]
        usage = pipe.repo.llm_usage()
        assert usage and usage[0]["cached"] >= 5


def test_nvidia_down_degrades_to_keyword_ranking(full_settings):
    with respx.mock as router:
        mock_sources(router)
        router.get(ND).respond(200, json=sn.newsdata_json())
        router.post(NV + "/embeddings").respond(503)
        chat = router.post(NV + "/chat/completions").respond(403, json={"status": 403, "detail": "Authorization failed"})
        pipe = Phase1Pipeline(full_settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
    stages = pipe.repo.stages(run_id)
    assert pipe.repo.get_run(run_id).status == "completed"
    assert "unavailable" in stages["cluster"].output["embeddings"]
    review = stages["review"].output
    assert review["status"] == "failed_all" and "Public API Endpoints" in review["reason"]
    assert chat.call_count == 1  # fatal error stops further calls
    assert pipe.repo.scores_for_run(run_id, selected_only=True)  # keyword ranking still produced a briefing


def test_without_keys_everything_still_works(settings):
    with respx.mock as router:
        mock_sources(router)
        nd = router.get(ND).respond(200, json=sn.newsdata_json())
        settings.ingestion.providers.newsdata.queries = [NewsdataQuery(topic="india", country="in")]
        settings.clustering.embeddings.enabled = True
        pipe = Phase1Pipeline(settings, stage_retry_delay=0)
        run_id = pipe.run(now=sn.NOW)
    stages = pipe.repo.stages(run_id)
    assert nd.call_count == 0 and stages["ingest"].output["providers"]["newsdata"]["note"] == "NEWSDATA_API_KEY not set"
    assert "NVIDIA_API_KEY not set" in stages["cluster"].output["embeddings"]
    assert stages["review"].output["reason"] == "NVIDIA_API_KEY is not set"
    assert pipe.repo.scores_for_run(run_id, selected_only=True)
