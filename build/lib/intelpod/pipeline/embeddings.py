"""Optional local embeddings through Ollama (free, runs on your machine).

API: POST {ollama_url}/api/embed  {"model": ..., "input": [texts]} -> {"embeddings": [[...], ...]}
If Ollama is not running or the model is missing, clustering silently falls back
to lexical similarity and the run log records why.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..config import EmbeddingsConfig

log = logging.getLogger(__name__)


class OllamaEmbedder:
    def __init__(self, cfg: EmbeddingsConfig, transport: httpx.BaseTransport | None = None, batch_size: int = 64):
        self.cfg = cfg
        self.model = f"ollama:{cfg.model}"
        self.batch_size = batch_size
        self._client = httpx.Client(base_url=cfg.ollama_url.rstrip("/"), timeout=cfg.timeout_seconds,
                                    transport=transport, trust_env=False)
        self.last_error: str | None = None

    def __call__(self, texts: list[str]) -> list[list[float]] | None:
        vectors: list[list[float]] = []
        try:
            for i in range(0, len(texts), self.batch_size):
                batch = texts[i: i + self.batch_size]
                r = self._client.post("/api/embed", json={"model": self.cfg.model, "input": batch})
                r.raise_for_status()
                embs = r.json().get("embeddings")
                if not isinstance(embs, list) or len(embs) != len(batch):
                    raise ValueError("unexpected embeddings payload")
                vectors.extend(_l2(v) for v in embs)
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("Ollama embeddings unavailable (%s); using lexical similarity only", self.last_error)
            return None
        return vectors


def _l2(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dense_cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class NvidiaEmbedder:
    """Embeddings from NVIDIA's hosted API (OpenAI-compatible POST {base_url}/embeddings).

    Retrieval embedding models there require `input_type` ("passage" for documents)
    and accept `truncate` (NONE | START | END). Any failure returns None so clustering
    falls back to lexical similarity; the reason is kept in `last_error`."""

    def __init__(self, cfg: EmbeddingsConfig, base_url: str, api_key_env: str,
                 transport: httpx.BaseTransport | None = None, requests_per_minute: float = 0, sleep=time.sleep):
        self.cfg = cfg
        self.models = [cfg.nvidia_model] + [m for m in cfg.nvidia_fallback_models if m != cfg.nvidia_model]
        self.model = self.models[0]
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._sleep = sleep
        self.api_key = os.environ.get(api_key_env)
        self.api_key_env = api_key_env
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=cfg.timeout_seconds, transport=transport)
        self.last_error: str | None = None
        self.requests = 0
        self._model_gone = False

    def __call__(self, texts: list[str]) -> list[list[float]] | None:
        """Embed with the first model that works. A model that is missing or retired
        (404/410) on the first request is replaced by the next configured one; a model is
        never switched mid-batch-set, so all vectors share one space."""
        if not self.api_key:
            self.last_error = f"{self.api_key_env} not set"
            return None
        errors = []
        for model in self.models:
            out = self._embed_with(model, texts)
            if out is not None:
                self.model = model
                return out
            errors.append(f"{model}: {self.last_error}")
            if not self._model_gone:
                break
        self.last_error = "; ".join(errors)[:500]
        return None

    def _embed_with(self, model: str, texts: list[str]) -> list[list[float]] | None:
        self._model_gone = False
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        vectors: list[list[float]] = []
        try:
            for i in range(0, len(texts), self.cfg.batch_size):
                if (i or self.requests) and self._interval:
                    self._sleep(self._interval)  # share the provider's per-minute budget with chat calls
                batch = [t[:2000] for t in texts[i: i + self.cfg.batch_size]]
                body = {"model": model, "input": batch, "input_type": self.cfg.input_type,
                        "truncate": self.cfg.truncate, "encoding_format": "float"}
                r = self._client.post("/embeddings", json=body, headers=headers)
                self.requests += 1
                if r.status_code in (404, 410) and i == 0:
                    self._model_gone = True
                if r.status_code >= 400:
                    raise ValueError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json().get("data")
                if not isinstance(data, list) or len(data) != len(batch):
                    raise ValueError("unexpected embeddings payload")
                data = sorted(data, key=lambda d: d.get("index", 0))
                vectors.extend(_l2(d["embedding"]) for d in data)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("NVIDIA embeddings unavailable (%s); using lexical similarity only", self.last_error)
            return None
        return vectors


class CachedEmbedder:
    """Wraps an embedder with a SQLite cache keyed by sha256(model, text).

    Runs overlap (a 36 h window run several times a day), so most headlines were
    already embedded; only new ones cost API requests. All-or-nothing: if any missing
    text cannot be embedded, returns None so clustering stays on one similarity scale."""

    def __init__(self, inner, repo, retention_days: int = 14):
        self.inner = inner
        self.repo = repo
        self.model = getattr(inner, "model", "unknown")
        self.retention = timedelta(days=retention_days)
        self.hits = 0
        self.misses = 0

    @property
    def last_error(self):
        return getattr(self.inner, "last_error", None)

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\x1f{text}".encode()).hexdigest()

    def __call__(self, texts: list[str]) -> list[list[float]] | None:
        keys = [self._key(t) for t in texts]
        cached = self.repo.embeddings_get(sorted(set(keys)))
        missing = [i for i, k in enumerate(keys) if k not in cached]
        self.hits, self.misses = len(texts) - len(missing), len(missing)
        if missing:
            new = self.inner([texts[i] for i in missing])
            if new is None:
                return None
            used = getattr(self.inner, "model", self.model)
            if used != self.model:
                # The inner embedder fell back to another model: vectors from different models
                # are not comparable, so embed everything with the new model.
                self.model = used
                keys = [self._key(t) for t in texts]
                new = self.inner(texts) if len(missing) != len(texts) else new
                if new is None:
                    return None
                missing = list(range(len(texts)))
                self.hits, self.misses = 0, len(texts)
                cached = {}
            fresh = {keys[i]: _pack(v) for i, v in zip(missing, new)}
            self.repo.embeddings_put(self.model, fresh)
            cached.update(fresh)
        try:
            self.repo.embeddings_prune(datetime.now(timezone.utc) - self.retention)
        except Exception:  # noqa: BLE001 - pruning is housekeeping only
            log.debug("embedding cache prune failed", exc_info=True)
        return [_unpack(cached[k]) for k in keys]


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def _unpack(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))
