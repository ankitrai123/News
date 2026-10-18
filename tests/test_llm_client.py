"""LLM client against a MOCKED NVIDIA-style API (respx). No network, no real model."""
import json

import httpx
import uuid

import pytest
import respx
from pydantic import BaseModel

from intelpod.config import LLMConfig
from intelpod.db import Database
from intelpod.llm.client import LLMClient, LLMError, LLMUnavailable
from intelpod.repository import Repository

BASE = "https://integrate.api.nvidia.com/v1"
CHAT = BASE + "/chat/completions"


class Answer(BaseModel):
    verdict: str
    score: float


FIRST = "nvidia/nemotron-3.5-lightning-30b-a3b"


def completion(content, model=FIRST, finish="stop", **msg):
    return {"id": "x", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content, **msg},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}


@pytest.fixture
def repo(tmp_path):
    return Repository(Database(tmp_path / "t.db"))


@pytest.fixture
def client(repo, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-secret-123456")
    return LLMClient(_cfg(max_retries=1), repo=repo, run_id=f"run-{uuid.uuid4().hex}", sleep=lambda s: None)


def _cfg(**kw) -> LLMConfig:
    """These tests were written for nemotron-first ordering; keep it explicit here."""
    from intelpod.config import DEFAULT_REVIEW_MODELS, LLMTaskConfig
    models = sorted(DEFAULT_REVIEW_MODELS, key=lambda m: m.id != FIRST)
    return LLMConfig(tasks={"event_review": LLMTaskConfig(temperature=0.2, max_tokens=900,
                                                          models=[m.model_copy() for m in models])}, **kw)


MSGS = [{"role": "user", "content": "Rate this."}]


def test_missing_key_is_unavailable(repo, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    c = LLMClient(LLMConfig(), repo=repo)
    assert c.availability() == (False, "NVIDIA_API_KEY is not set")
    with pytest.raises(LLMUnavailable):
        c.chat("event_review", MSGS)


@respx.mock
def test_request_shape_auth_structured_output_and_thinking_switch(client):
    route = respx.post(CHAT).respond(200, json=completion('{"verdict": "ok", "score": 0.5}'))
    obj, model = client.chat_json("event_review", MSGS, Answer)
    assert obj == Answer(verdict="ok", score=0.5) and model == FIRST
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer nvapi-test-secret-123456"
    body = json.loads(req.content)
    assert body["model"] == FIRST and body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["properties"]["score"]["type"] == "number"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "top_p" not in body


@respx.mock
def test_think_tags_stripped_and_usage_logged(client, repo):
    respx.post(CHAT).respond(200, json=completion('<think>hmm</think>{"verdict": "a", "score": 1}'))
    obj, _ = client.chat_json("event_review", MSGS, Answer)
    assert obj.verdict == "a"
    calls = repo.llm_calls()
    assert len(calls) == 1 and calls[0].ok and calls[0].prompt_tokens == 120 and calls[0].task == "event_review"


@respx.mock
def test_cache_prevents_second_call(client, repo):
    route = respx.post(CHAT).respond(200, json=completion('{"verdict": "a", "score": 1}'))
    client.chat_json("event_review", MSGS, Answer)
    client.chat_json("event_review", MSGS, Answer)
    assert route.call_count == 1
    assert [c.cached for c in reversed(repo.llm_calls())] == [False, True]


@respx.mock
def test_invalid_json_gets_one_repair_attempt(client):
    route = respx.post(CHAT).mock(side_effect=[
        httpx.Response(200, json=completion("The verdict is fine")),
        httpx.Response(200, json=completion('{"verdict": "fine", "score": 0.2}'))])
    obj, _ = client.chat_json("event_review", MSGS, Answer)
    assert obj.score == 0.2 and route.call_count == 2
    repair = json.loads(route.calls.last.request.content)["messages"]
    assert repair[-1]["role"] == "user" and "not valid JSON" in repair[-1]["content"]


@respx.mock
def test_structured_output_rejected_falls_back_to_prompt_only(client):
    route = respx.post(CHAT).mock(side_effect=[
        httpx.Response(422, json={"error": "body -> response_format\n  Extra inputs are not permitted"}),
        httpx.Response(200, json=completion('{"verdict": "x", "score": 0}'))])
    obj, _ = client.chat_json("event_review", MSGS, Answer)
    assert obj.verdict == "x"
    assert "response_format" not in json.loads(route.calls.last.request.content)


@respx.mock
@pytest.mark.parametrize("status,body,phrase", [
    (401, {"status": 401, "title": "Unauthorized", "detail": "Authentication failed"}, "Authentication failed"),
    (403, {"status": 403, "title": "Forbidden", "detail": "Authorization failed"}, "help@build.nvidia.com"),
])
def test_fatal_errors_are_unavailable_with_guidance(client, status, body, phrase):
    respx.post(CHAT).respond(status, json=body)
    with pytest.raises(LLMUnavailable, match=phrase):
        client.chat("event_review", MSGS)


@respx.mock
def test_rate_limit_retried_then_unavailable(client):
    route = respx.post(CHAT).respond(429, json={"status": 429, "title": "Too Many Requests"})
    with pytest.raises(LLMUnavailable, match="rate limited"):
        client.chat("event_review", MSGS)
    assert route.call_count == 2  # max_retries=1


@respx.mock
def test_202_queued_request_is_polled(client):
    respx.post(CHAT).respond(202, headers={"NVCF-REQID": "req-42"})
    poll = respx.get("https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/req-42").mock(side_effect=[
        httpx.Response(202, headers={"NVCF-REQID": "req-42"}),
        httpx.Response(200, json=completion('{"verdict": "late", "score": 0.3}'))])
    obj, _ = client.chat_json("event_review", MSGS, Answer)
    assert obj.verdict == "late" and poll.call_count == 2


@respx.mock
def test_reasoning_only_answer_explains_fix(client):
    respx.post(CHAT).respond(200, json=completion("", finish="length", reasoning_content="long thoughts..."))
    with pytest.raises(LLMError, match="max_tokens or disable thinking"):
        client.chat("event_review", MSGS)


@respx.mock
def test_truncated_answer_not_cached(client, repo):
    route = respx.post(CHAT).respond(200, json=completion('{"verdict": "a", "sc', finish="length"))
    with pytest.raises(Exception):
        client.chat_json("event_review", MSGS, Answer)
    from sqlalchemy import func, select
    from intelpod.db import LLMCacheRow
    with repo.db.session() as s:
        assert s.scalar(select(func.count()).select_from(LLMCacheRow)) == 0
    assert route.call_count == 2  # original + repair; neither cached


@respx.mock
def test_api_key_never_logged(client, repo, caplog):
    respx.post(CHAT).respond(401, json={"detail": "Authentication failed"})
    with pytest.raises(LLMUnavailable):
        client.chat("event_review", MSGS)
    assert "nvapi-test-secret" not in caplog.text
    assert all("nvapi-test-secret" not in (c.error or "") for c in repo.llm_calls())


@respx.mock
def test_list_models(client):
    respx.get(BASE + "/models").respond(200, json={"object": "list", "data": [{"id": "b/m"}, {"id": "a/m"}]})
    assert client.list_models() == ["a/m", "b/m"]


@respx.mock
def test_ollama_provider_needs_no_key(repo):
    from intelpod.config import LLMModelChoice
    cfg = LLMConfig(provider="ollama")
    cfg.tasks["event_review"].models = [LLMModelChoice(id="qwen2.5:7b")]
    route = respx.post("http://localhost:11434/v1/chat/completions").respond(
        200, json=completion('{"verdict": "local", "score": 0.1}', model="qwen2.5:7b"))
    c = LLMClient(cfg, repo=repo, sleep=lambda s: None)
    assert c.availability() == (True, "ok")
    obj, _ = c.chat_json("event_review", MSGS, Answer)
    assert obj.verdict == "local"
    assert "Authorization" not in route.calls.last.request.headers
    assert json.loads(route.calls.last.request.content)["response_format"] == {"type": "json_object"}


@respx.mock
def test_retired_model_fails_over_to_next(client, repo):
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == FIRST:
            return httpx.Response(410, json={"status": 410, "title": "Gone",
                                             "detail": f"{FIRST} has reached its end of life"})
        if model == "openai/gpt-oss-20b":
            return httpx.Response(404, json={"detail": "Function 'x': Not found for account 'y'"})
        return httpx.Response(200, json=completion('{"verdict": "g", "score": 0.4}', model=model))

    respx.post(CHAT).mock(side_effect=handler)
    obj, model = client.chat_json("event_review", MSGS, Answer)
    assert model == "google/gemma-4-31b-it" and obj.verdict == "g"
    client.chat_json("event_review", [{"role": "user", "content": "again"}], Answer)
    assert seen == [FIRST, "openai/gpt-oss-20b", "google/gemma-4-31b-it", "google/gemma-4-31b-it"]


@respx.mock
def test_all_models_gone_is_unavailable_with_guidance(client):
    respx.post(CHAT).respond(410, json={"status": 410, "title": "Gone"})
    with pytest.raises(LLMUnavailable, match="llm models"):
        client.chat("event_review", MSGS)


@respx.mock
def test_forbidden_field_dropped_per_model(client):
    route = respx.post(CHAT).mock(side_effect=[
        httpx.Response(422, json={"error": "body -> chat_template_kwargs\n  Extra inputs are not permitted (type=extra_forbidden)"}),
        httpx.Response(200, json=completion('{"verdict": "x", "score": 0}'))])
    client.chat_json("event_review", MSGS, Answer)
    body = json.loads(route.calls.last.request.content)
    assert "chat_template_kwargs" not in body and "response_format" in body


@respx.mock
def test_per_model_settings_applied(client):
    def handler(request):
        b = json.loads(request.content)
        if b["model"] == FIRST:
            return httpx.Response(410, json={"title": "Gone"})
        assert b["reasoning_effort"] == "low" and b["max_tokens"] == 2000 and "chat_template_kwargs" not in b
        return httpx.Response(200, json=completion('{"verdict": "o", "score": 0.1}', model=b["model"]))
    respx.post(CHAT).mock(side_effect=handler)
    assert client.chat_json("event_review", MSGS, Answer)[1] == "openai/gpt-oss-20b"


@respx.mock
def test_server_errors_and_timeouts_fail_over(client):
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == FIRST:
            return httpx.Response(503, json={"title": "Service Unavailable"})
        if model == "openai/gpt-oss-20b":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=completion('{"verdict": "ok", "score": 1}', model=model))

    respx.post(CHAT).mock(side_effect=handler)
    obj, model = client.chat_json("event_review", MSGS, Answer)
    assert model == "google/gemma-4-31b-it"
    assert seen.count(FIRST) == 2 and seen.count("openai/gpt-oss-20b") == 1  # 503 retried once; timeout not retried


@respx.mock
def test_all_models_failing_is_unavailable(client):
    respx.post(CHAT).respond(500, json={"title": "boom"})
    with pytest.raises(LLMUnavailable, match="no configured model"):
        client.chat("event_review", MSGS)


@respx.mock
def test_invalid_answers_are_never_cached(client, repo):
    route = respx.post(CHAT).respond(200, json=completion("not json at all"))
    for _ in range(2):
        with pytest.raises(Exception):
            client.chat_json("event_review", MSGS, Answer)
    assert route.call_count == 4  # each run asks the model again (original + repair)
    route.respond(200, json=completion('{"verdict": "fixed", "score": 0.3}'))
    assert client.chat_json("event_review", MSGS, Answer)[0].verdict == "fixed"


@respx.mock
def test_all_404_points_at_entitlement(client):
    respx.post(CHAT).respond(404, json={"detail": "Function 'x': Not found for account 'y'"})
    with pytest.raises(LLMUnavailable, match="Public API Endpoints"):
        client.chat("event_review", MSGS)


def test_failed_model_is_skipped_for_the_rest_of_the_run():
    from intelpod.llm.client import _dead_for_run
    a, b = _dead_for_run("run-x"), _dead_for_run("run-x")
    a.add("slow/model")
    assert "slow/model" in b and "slow/model" not in _dead_for_run("run-y") and _dead_for_run(None) == set()
