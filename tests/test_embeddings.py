"""NVIDIA embedder + cache against a MOCKED API (respx). No network."""
import json

import httpx
import pytest
import respx

from intelpod.config import EmbeddingsConfig
from intelpod.db import Database
from intelpod.pipeline.embeddings import CachedEmbedder, NvidiaEmbedder
from intelpod.repository import Repository

URL = "https://integrate.api.nvidia.com/v1/embeddings"


def handler_factory(dead=(), dim=4, log=None):
    def handler(request):
        body = json.loads(request.content)
        if log is not None:
            log.append((body["model"], len(body["input"])))
        if body["model"] in dead:
            return httpx.Response(410, json={"title": "Gone"})
        base = 1.0 if "e5" in body["model"] else 2.0
        # deliberately return out of order to check sorting by index
        data = [{"index": i, "embedding": [base, float(len(t)), 0.0, 1.0][:dim]} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data)), "model": body["model"]})
    return handler


@pytest.fixture
def emb(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-x")
    cfg = EmbeddingsConfig(batch_size=2)
    return NvidiaEmbedder(cfg, "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", sleep=lambda s: None)


@respx.mock
def test_batches_sorted_and_normalised(emb):
    calls = []
    respx.post(URL).mock(side_effect=handler_factory(log=calls))
    out = emb(["a", "bbb", "cc"])
    assert calls == [("nvidia/nemotron-3-embed-1b", 2), ("nvidia/nemotron-3-embed-1b", 1)]
    assert len(out) == 3 and all(abs(sum(x * x for x in v) - 1) < 1e-6 for v in out)
    assert out[0][1] < out[1][1]  # "a" (len 1) before "bbb" (len 3): index order restored


@respx.mock
def test_retired_model_falls_back(emb):
    calls = []
    respx.post(URL).mock(side_effect=handler_factory(dead={"nvidia/nemotron-3-embed-1b"}, log=calls))
    out = emb(["a", "b"])
    assert out and emb.model == "nvidia/nv-embedqa-e5-v5"
    assert [c[0] for c in calls] == ["nvidia/nemotron-3-embed-1b", "nvidia/nv-embedqa-e5-v5"]


@respx.mock
def test_transient_failure_is_retried_and_does_not_switch_model(emb):
    calls = []
    respx.post(URL).mock(side_effect=lambda r: (calls.append(json.loads(r.content)["model"]), httpx.Response(503))[1])
    assert emb(["a"]) is None and "503" in emb.last_error
    assert calls == ["nvidia/nemotron-3-embed-1b"] * 4  # 3 retries, never the fallback model


@respx.mock
def test_rate_limit_midway_keeps_finished_batches(emb, tmp_path):
    repo = Repository(Database(tmp_path / "p.db"))
    n = {"i": 0}
    good = handler_factory()

    def flaky(request):
        n["i"] += 1
        return httpx.Response(429, json={"title": "Too Many Requests"}) if n["i"] >= 3 else good(request)

    respx.post(URL).mock(side_effect=flaky)
    cached = CachedEmbedder(emb, repo)
    assert cached(["a", "b", "c", "d", "e", "f"]) is None   # batch 3 of 3 never succeeds
    respx.post(URL).mock(side_effect=handler_factory())
    out = cached(["a", "b", "c", "d", "e", "f"])
    assert out and cached.hits == 4 and cached.misses == 2   # batches 1-2 were kept


def test_too_few_vectors_disable_embeddings():
    from intelpod.config import ClusteringConfig
    from intelpod.pipeline.cluster import Clusterer, dense_baseline

    class D:
        def __init__(self, v):
            self.dense = v
    assert dense_baseline([D([1.0, 0.0]), D([0.9, 0.1]), D([0.8, 0.2])]) == 1.0
    c = Clusterer(ClusteringConfig())
    c.baseline = 1.0
    assert c._lexical(0.2, 0.95) == 0.2


def test_missing_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    e = NvidiaEmbedder(EmbeddingsConfig(), "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY")
    assert e(["a"]) is None and e.last_error == "NVIDIA_API_KEY not set"


@respx.mock
def test_cache_hits_and_model_switch_reembeds_everything(emb, tmp_path):
    repo = Repository(Database(tmp_path / "e.db"))
    calls = []
    respx.post(URL).mock(side_effect=handler_factory(log=calls))
    cached = CachedEmbedder(emb, repo)
    first = cached(["a", "bb"])
    assert cached.misses == 2
    again = cached(["a", "bb", "ccc"])
    assert cached.hits == 2 and cached.misses == 1
    assert all(a == pytest.approx(b, rel=1e-6) for a, b in zip(again[:2], first))  # float32 round-trip

    # Primary model retires: the newly missing text triggers a fallback, and because vectors
    # from two models are not comparable, everything is re-embedded with the fallback model.
    calls.clear()
    respx.post(URL).mock(side_effect=handler_factory(dead={"nvidia/nemotron-3-embed-1b"}, log=calls))
    emb.model = emb.models[0]
    out = cached(["a", "bb", "dddd"])
    # "dddd" was embedded (and stored) by the fallback; "a" and "bb" are re-embedded with it.
    assert cached.model == "nvidia/nv-embedqa-e5-v5" and cached.misses == 2 and len(out) == 3

    # Next run: the fallback's cached vectors are found and reused; no request is needed.
    calls.clear()
    fresh = NvidiaEmbedder(EmbeddingsConfig(batch_size=2), "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
                           sleep=lambda s: None)
    again = CachedEmbedder(fresh, repo)(["a", "bb", "dddd"])
    assert again and calls == []
    # The fake e5 vectors have component0 == component3 (base 1.0); nemotron ones have 2:1.
    assert all(v[0] == pytest.approx(v[3]) for v in out)  # every vector from the e5 space
