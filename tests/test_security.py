"""Security/privacy regressions found in review (all offline, mocked HTTP)."""
import json

import httpx
import pytest
import respx

from fixtures import synthetic_news as sn
from intelpod.config import HttpConfig, LLMConfig, NewsdataProviderConfig, NewsdataQuery
from intelpod.db import Database
from intelpod.http import HttpClient
from intelpod.llm.client import LLMClient, LLMError
from intelpod.logging_setup import read_secret, redact
from intelpod.providers.newsdata import NewsdataProvider
from intelpod.repository import Repository

SECRET = "nvapi-SUPERSECRETKEY123"


def test_read_secret_strips_and_rejects_bad_values(monkeypatch, caplog):
    monkeypatch.setenv("K1", f"  {SECRET}\n")
    assert read_secret("K1") == SECRET
    monkeypatch.setenv("K2", '"abc def ghi"')
    assert read_secret("K2") is None and "abc" not in caplog.text
    monkeypatch.setenv("K3", "")
    assert read_secret("K3") is None and read_secret(None) is None


def test_redact_removes_secret_values(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET + "\n")
    assert SECRET not in redact(f"Illegal header value b'Bearer {SECRET}\\n'")


@respx.mock
def test_whitespace_key_never_leaks_into_db(monkeypatch, tmp_path):
    """Keys with a trailing newline used to produce an h11 error containing the full key."""
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET + "\n")
    monkeypatch.setenv("NEWSDATA_API_KEY", "pub_NEWSDATASECRET99 \n")
    repo = Repository(Database(tmp_path / "s.db"))
    respx.post("https://integrate.api.nvidia.com/v1/chat/completions").respond(
        200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
    respx.get("https://newsdata.io/api/1/latest").respond(200, json=sn.newsdata_json())
    client = LLMClient(LLMConfig(max_retries=0), repo=repo, sleep=lambda s: None)
    try:
        client.chat("event_review", [{"role": "user", "content": "x"}])
    except LLMError:
        pass
    http = HttpClient(HttpConfig(max_retries=0, per_host_min_interval_seconds=0, respect_robots_txt=False), "t")
    nd = NewsdataProvider(http, NewsdataProviderConfig(min_interval_seconds=0, queries=[NewsdataQuery(topic="x", q="y")]))
    res = nd.fetch()
    repo.log_fetches("r", res.fetches)
    dump = json.dumps([c.error for c in repo.llm_calls()] + [f.error for f in repo.fetch_log()] + [f.target for f in repo.fetch_log()])
    assert "SUPERSECRETKEY" not in dump and "NEWSDATASECRET" not in dump
    # the stripped key is what gets sent
    sent = respx.calls[0].request.headers["Authorization"]
    assert sent == f"Bearer {SECRET}"


@respx.mock
def test_api_providers_get_no_contact_email(settings, monkeypatch):
    from intelpod.pipeline.runner import Phase1Pipeline
    from test_pipeline import mock_sources
    monkeypatch.setenv("NEWSDATA_API_KEY", "nd-k")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-k")
    settings.app.user_agent = "IntelPod/0.1 (contact: jane.doe@example.org)"
    settings.ingestion.providers.newsdata.queries = [NewsdataQuery(topic="india", country="in")]
    settings.llm.providers["nvidia"].requests_per_minute = 0
    settings.clustering.embeddings.enabled = False
    mock_sources(respx)
    nd = respx.get("https://newsdata.io/api/1/latest").respond(200, json=sn.newsdata_json())
    chat = respx.post("https://integrate.api.nvidia.com/v1/chat/completions").respond(403, json={"detail": "x"})
    Phase1Pipeline(settings, stage_retry_delay=0).run(now=sn.NOW)
    assert "jane.doe" not in nd.calls.last.request.headers["User-Agent"]
    assert "jane.doe" not in chat.calls.last.request.headers["User-Agent"]
    feed_calls = [c for c in respx.calls if "example-news.test" in str(c.request.url)]
    assert feed_calls and "jane.doe" in feed_calls[0].request.headers["User-Agent"]  # publishers still see contact


@respx.mock
def test_no_llm_sends_nothing_to_nvidia(settings, monkeypatch):
    from intelpod.pipeline.runner import Phase1Pipeline
    from test_pipeline import mock_sources
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-k")
    settings.llm.enabled = False          # what --no-llm sets
    settings.clustering.embeddings.enabled = True
    mock_sources(respx)
    nv = respx.route(host="integrate.api.nvidia.com").respond(500)
    pipe = Phase1Pipeline(settings, stage_retry_delay=0)
    run_id = pipe.run(now=sn.NOW)
    assert nv.call_count == 0
    assert "llm.enabled is false" in pipe.repo.stages(run_id)["cluster"].output["embeddings"]


def test_invalid_header_error_never_echoes_value():
    """httpx/h11 reject an illegal header value with a message that contains it (the key)."""
    from intelpod.http import FetchError

    def boom(request):
        raise httpx.LocalProtocolError("Illegal header value b'pub_UNKNOWNSECRET\\n'")

    http = HttpClient(HttpConfig(max_retries=2, per_host_min_interval_seconds=0, respect_robots_txt=False), "t",
                      transport=httpx.MockTransport(boom), sleep=lambda s: None)
    with pytest.raises(FetchError) as ei:
        http.get("https://x.test/a", check_robots=False)
    assert "UNKNOWNSECRET" not in str(ei.value) and "LocalProtocolError" in str(ei.value)
