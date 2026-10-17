"""Optional local embeddings through Ollama (free, runs on your machine).

API: POST {ollama_url}/api/embed  {"model": ..., "input": [texts]} -> {"embeddings": [[...], ...]}
If Ollama is not running or the model is missing, clustering silently falls back
to lexical similarity and the run log records why.
"""
from __future__ import annotations

import logging
import math

import httpx

from ..config import EmbeddingsConfig

log = logging.getLogger(__name__)


class OllamaEmbedder:
    def __init__(self, cfg: EmbeddingsConfig, transport: httpx.BaseTransport | None = None, batch_size: int = 64):
        self.cfg = cfg
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
