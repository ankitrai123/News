"""OpenAI-compatible chat/embeddings client for hosted (NVIDIA) or local (Ollama) models.

Responsibilities:
  * read the API key from the environment variable named in config (never from files
    in the repo, never logged),
  * client-side rate limiting (requests per minute) on top of HttpClient retries for
    429/5xx with Retry-After,
  * response cache in SQLite so resumed runs and re-renders never pay twice,
  * one log row per call in `llm_calls` (tokens, latency, errors) for quota tracking,
  * JSON answers: request structured output when the provider supports it, then
    extract + validate with pydantic, with one repair round-trip on invalid output.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, ValidationError

import re

from ..config import HttpConfig, LLMConfig, LLMModelChoice, LLMProviderConfig, LLMTaskConfig
from ..http import FetchError, HttpClient
from ..logging_setup import read_secret, redact
from .parsing import extract_json, strip_reasoning

log = logging.getLogger(__name__)


class LLMError(Exception):
    fatal = False


class LLMUnavailable(LLMError):
    """Provider unusable for this run (no key, bad key, no credits, unreachable). Stop calling."""

    fatal = True


class ModelGone(LLMError):
    """This model is not callable (404 not found for account / 410 retired); try the next one."""


class ModelFailed(LLMError):
    """This model kept failing (5xx after retries, timeout, broken connection); try the next one."""


@dataclass
class ChatResult:
    text: str
    model: str
    cached: bool = False
    cache_key: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class RateLimiter:
    def __init__(self, per_minute: float, sleep=time.sleep):
        self.interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()
        self._sleep = sleep

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if wait:
            self._sleep(wait)


class LLMClient:
    def __init__(self, cfg: LLMConfig, http: HttpClient | None = None, repo=None, run_id: str | None = None,
                 user_agent: str = "IntelPod", sleep=time.sleep):
        self.cfg = cfg
        self.provider = cfg.provider
        if cfg.provider not in cfg.providers:
            raise ValueError(f"llm.provider '{cfg.provider}' is not defined under llm.providers")
        self.pcfg: LLMProviderConfig = cfg.providers[cfg.provider]
        self.http = http or HttpClient(HttpConfig(timeout_seconds=cfg.timeout_seconds, max_retries=cfg.max_retries,
                                                  backoff_base_seconds=self.pcfg.backoff_base_seconds,
                                                  per_host_min_interval_seconds=0, respect_robots_txt=False,
                                                  retry_timeouts=False),
                                       user_agent, sleep=sleep)
        self.repo = repo
        self.run_id = run_id
        self.limiter = RateLimiter(self.pcfg.requests_per_minute, sleep=sleep)
        self._no_structured: set[str] = set() if self.pcfg.structured_output != "none" else _ALL
        self._forbidden_fields: dict[str, set[str]] = {}
        self._dead_models: set[str] = set()
        self._dead_404: set[str] = set()
        self._sleep = sleep

    # -- availability --------------------------------------------------------
    @property
    def api_key(self) -> str | None:
        return read_secret(self.pcfg.api_key_env)

    def availability(self) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, "llm.enabled is false"
        if self.pcfg.api_key_env and not self.api_key:
            return False, f"{self.pcfg.api_key_env} is not set"
        return True, "ok"

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def task(self, name: str) -> LLMTaskConfig:
        if name not in self.cfg.tasks:
            raise LLMError(f"no model configured for task '{name}' (llm.tasks.{name})")
        return self.cfg.tasks[name]

    # -- chat ------------------------------------------------------------------
    def chat(self, task: str, messages: list[dict], json_schema: dict | None = None,
             write_cache: bool = True) -> ChatResult:
        """Try the task's models in order. A model that is missing/retired (404/410) or keeps
        failing (5xx after retries, timeout) is skipped for the rest of this client's life
        (one pipeline run) and the next model is tried. `write_cache=False` lets the caller
        cache only after validating the answer."""
        ok, why = self.availability()
        if not ok:
            raise LLMUnavailable(why)
        t = self.task(task)
        last: LLMError | None = None
        for choice in t.models:
            if choice.id in self._dead_models:
                continue
            try:
                return self._chat_one(task, t, choice, messages, json_schema, write_cache)
            except (ModelGone, ModelFailed) as e:
                self._dead_models.add(choice.id)
                if isinstance(e, ModelGone) and str(e).split(": ", 1)[-1].startswith("404"):
                    self._dead_404.add(choice.id)
                log.warning("model %s unavailable (%s); trying the next configured model", choice.id, e)
                last = e
        hint = f"Run `intelpod llm models` and update llm.tasks.{task}.models."
        if self.provider == "nvidia" and self._dead_404 and self._dead_404 >= set(m.id for m in t.models):
            hint += (" Every model answered 404 'not found for account': if `intelpod llm models` lists them, the key "
                     "probably lacks the 'Public API Endpoints' entitlement (ask help@build.nvidia.com).")
        raise LLMUnavailable(f"no configured model for '{task}' is available on {self.provider} "
                             f"({last or 'all skipped'}). {hint}")

    def _chat_one(self, task: str, t: LLMTaskConfig, choice: LLMModelChoice, messages: list[dict],
                  json_schema: dict | None, write_cache: bool = True) -> ChatResult:
        model = choice.id
        body: dict = {"model": model, "messages": messages, "temperature": t.temperature,
                      "max_tokens": choice.max_tokens or t.max_tokens, "stream": False}
        if t.top_p is not None:  # NVIDIA advises changing temperature OR top_p, not both
            body["top_p"] = t.top_p
        if json_schema is not None and model not in self._no_structured:
            body.update(self._structured_output(json_schema))
        body.update(self.pcfg.extra_body)
        body.update(choice.extra_body)
        for f in self._forbidden_fields.get(model, ()):
            body.pop(f, None)
        key = hashlib.sha256(json.dumps([self.provider, self.pcfg.base_url, body], sort_keys=True).encode()).hexdigest()

        if self.cfg.cache and self.repo is not None:
            hit = self.repo.llm_cache_get(key)
            if hit is not None:
                self._log(task, model, ok=True, cached=True, duration_ms=0)
                return ChatResult(text=hit, model=model, cached=True, cache_key=key)

        self.limiter.wait()
        t0 = time.monotonic()
        url = self.pcfg.base_url.rstrip("/") + "/chat/completions"
        try:
            resp = self.http.post(url, json=body, headers=self._headers(), timeout=self.cfg.timeout_seconds)
        except FetchError as e:
            status = e.status_code
            detail = _error_detail(e.response) if e.response is not None else str(e)
            self._log(task, model, ok=False, duration_ms=_ms(t0), status_code=status, error=detail)
            if status == 429:  # account-wide rate limit: other models would hit it too
                raise LLMUnavailable(f"rate limited by {self.provider} after retries: {detail}") from e
            if status is None and detail.startswith(("ProxyError", "UnsupportedProtocol")):
                raise LLMUnavailable(f"{self.provider} unreachable: {detail}") from e
            raise ModelFailed(f"{model}: {detail if status is None else f'error {status}: {detail}'}") from e
        if resp.status_code == 202:
            resp = self._poll_pending(resp)
        duration = _ms(t0)
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            self._log(task, model, ok=False, duration_ms=duration, status_code=resp.status_code, error=detail)
            if resp.status_code in (400, 422):
                field = _forbidden_field(detail)
                if field and field not in self._forbidden_fields.get(model, set()) and field in body:
                    # e.g. {"error": "body -> chat_template_kwargs ... extra_forbidden"}: drop it and retry once.
                    log.warning("%s does not accept '%s'; retrying without it", model, field)
                    self._forbidden_fields.setdefault(model, set()).add(field)
                    return self._chat_one(task, t, choice, messages, json_schema, write_cache)
                if json_schema is not None and model not in self._no_structured:
                    log.warning("%s rejected structured output (%s); retrying with prompt-only JSON", model, detail[:120])
                    self._no_structured.add(model)
                    return self._chat_one(task, t, choice, messages, json_schema, write_cache)
            if resp.status_code in (401, 402, 403):
                hint = (" The key may lack the 'Public API Endpoints' entitlement; if it cannot be selected when "
                        "creating a key at build.nvidia.com/settings/api-keys, NVIDIA support (help@build.nvidia.com) "
                        "must enable it." if self.provider == "nvidia" else "")
                raise LLMUnavailable(f"{self.provider} rejected the request ({resp.status_code}): {detail}.{hint}")
            if resp.status_code in (404, 410):
                raise ModelGone(f"{model}: {resp.status_code} {detail}")
            if resp.status_code >= 500 or resp.status_code == 202:  # 202 = still queued after polling
                raise ModelFailed(f"{model}: error {resp.status_code}: {detail}")
            raise LLMError(f"{self.provider} error {resp.status_code}: {detail}")
        try:
            data = resp.json()
            first = data["choices"][0]
            msg = first["message"]
            text = msg.get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            self._log(task, model, ok=False, duration_ms=duration, status_code=resp.status_code,
                      error=f"unexpected response shape: {e}")
            raise LLMError(f"unexpected response from {self.provider}: {e}") from e
        usage = data.get("usage") or {}
        text = strip_reasoning(text)
        finish = first.get("finish_reason")
        if not text and (msg.get("reasoning_content") or msg.get("reasoning")):
            self._log(task, model, ok=False, duration_ms=duration, status_code=resp.status_code,
                      error=f"only reasoning returned (finish_reason={finish})")
            raise LLMError(f"{model} spent its output on reasoning (finish_reason={finish}); raise max_tokens "
                           f"or disable thinking for it in llm.tasks.{task}.models")
        self._log(task, model, ok=True, duration_ms=duration, status_code=resp.status_code,
                  prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                  error=f"finish_reason={finish}" if finish not in (None, "stop") else None)
        if not text:
            raise LLMError(f"empty answer from {model} (finish_reason={finish})")
        complete = finish in (None, "stop")
        if write_cache and complete:
            self._cache_put(key, model, text)
        return ChatResult(text=text, model=model, prompt_tokens=usage.get("prompt_tokens"),
                          completion_tokens=usage.get("completion_tokens"), cache_key=key if complete else None)

    def _cache_put(self, key: str | None, model: str, text: str) -> None:
        if key and self.cfg.cache and self.repo is not None:
            self.repo.llm_cache_put(key, self.provider, model, text)

    def chat_json(self, task: str, messages: list[dict], schema: type[BaseModel]) -> tuple[BaseModel, str]:
        """Ask for JSON matching `schema`; validate; one repair attempt. Returns (object, model)."""
        js = schema.model_json_schema()
        res = self.chat(task, messages, json_schema=js, write_cache=False)
        try:
            obj = schema.model_validate(extract_json(res.text))
        except (ValueError, ValidationError) as first_error:
            repair = messages + [
                {"role": "assistant", "content": res.text[:4000]},
                {"role": "user", "content": "Your answer was not valid JSON for the required format: "
                                            f"{str(first_error)[:500]}. Reply with only the corrected JSON object."},
            ]
            res2 = self.chat(task, repair, json_schema=js, write_cache=False)
            obj = schema.model_validate(extract_json(res2.text))
            if not res2.cached:
                self._cache_put(res2.cache_key, res2.model, res2.text)
            return obj, res2.model
        # Cache only answers that validated, so an invalid answer is never replayed on later runs.
        if not res.cached:
            self._cache_put(res.cache_key, res.model, res.text)
        return obj, res.model

    def _poll_pending(self, resp, max_wait: float | None = None):
        """NVIDIA may answer 202 + NVCF-REQID for queued requests; poll until it completes."""
        req_id = resp.headers.get("NVCF-REQID")
        if not req_id or not self.pcfg.poll_url:
            return resp
        deadline = time.monotonic() + (max_wait or self.cfg.timeout_seconds)
        url = self.pcfg.poll_url.rstrip("/") + "/" + req_id
        while resp.status_code == 202 and time.monotonic() < deadline:
            self._sleep(1.0)
            resp = self.http.get(url, headers=self._headers(), check_robots=False)
        return resp

    def _structured_output(self, schema: dict) -> dict:
        mode = self.pcfg.structured_output
        if mode == "guided_json":
            return {"guided_json": schema}  # top-level form; NIM 1.14 deprecated the nvext wrapper
        if mode == "json_schema":
            return {"response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}}
        if mode == "json_object":
            return {"response_format": {"type": "json_object"}}
        return {}

    # -- models ----------------------------------------------------------------
    def list_models(self) -> list[str]:
        ok, why = self.availability()
        if not ok:
            raise LLMUnavailable(why)
        resp = self.http.get(self.pcfg.base_url.rstrip("/") + "/models", headers=self._headers(), check_robots=False)
        if resp.status_code >= 400:
            raise LLMError(f"{self.provider} /models error {resp.status_code}: {_error_detail(resp)}")
        return sorted(m.get("id", "") for m in resp.json().get("data", []) if isinstance(m, dict))

    def _log(self, task: str, model: str, ok: bool, duration_ms: int, cached: bool = False, **kw) -> None:
        if self.repo is None:
            return
        try:
            self.repo.log_llm_call(run_id=self.run_id, provider=self.provider, model=model, task=task, ok=ok,
                                   cached=cached, duration_ms=duration_ms, started_at=datetime.now(timezone.utc), **kw)
        except Exception:  # noqa: BLE001 - logging must never break a call
            log.debug("could not record llm call", exc_info=True)


class _AllSet(set):
    """Membership test that is always true (structured output disabled for every model)."""

    def __contains__(self, item) -> bool:
        return True


_ALL = _AllSet()


def _forbidden_field(detail: str) -> str | None:
    m = re.search(r"body\s*->\s*([A-Za-z_]+)[^\n]*\n?[^\n]*extra_forbidden|body\s*->\s*([A-Za-z_]+)\s+Extra inputs",
                  detail or "")
    if m:
        return m.group(1) or m.group(2)
    m = re.search(r"body\s*->\s*([A-Za-z_]+)", detail or "")
    return m.group(1) if m and "not permitted" in (detail or "") else None


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _error_detail(resp) -> str:
    return redact(_raw_error_detail(resp))


def _raw_error_detail(resp) -> str:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or "")[:300]
    if isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), (dict, str)) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        return str(data.get("detail") or data.get("title") or data)[:300]
    return str(data)[:300]
