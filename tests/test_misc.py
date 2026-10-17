import logging

import httpx
import respx

from intelpod.config import EmbeddingsConfig, load_settings
from intelpod.logging_setup import RedactingFilter
from intelpod.pipeline.embeddings import OllamaEmbedder


def test_secrets_redacted_from_logs(monkeypatch):
    monkeypatch.setenv("NEWSAPI_KEY", "supersecret123")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "GET https://a.test/?apiKey=abc123&q=1 with supersecret123", (), None)
    RedactingFilter().filter(rec)
    msg = rec.getMessage()
    assert "abc123" not in msg and "supersecret123" not in msg and "apiKey=***" in msg


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("INTELPOD_GDELT_ENABLED", "false")
    monkeypatch.setenv("INTELPOD_LOOKBACK_HOURS", "12")
    s = load_settings()
    assert s.ingestion.providers.gdelt.enabled is False and s.ingestion.lookback_hours == 12


@respx.mock
def test_ollama_embedder_ok_and_unavailable():
    cfg = EmbeddingsConfig(enabled=True, ollama_url="http://ollama.test:11434")
    respx.post("http://ollama.test:11434/api/embed").respond(200, json={"embeddings": [[3.0, 4.0], [0.0, 2.0]]})
    vecs = OllamaEmbedder(cfg)(["a", "b"])
    assert vecs == [[0.6, 0.8], [0.0, 1.0]]
    respx.post("http://ollama.test:11434/api/embed").mock(side_effect=httpx.ConnectError("refused"))
    emb = OllamaEmbedder(cfg)
    assert emb(["a"]) is None and "ConnectError" in emb.last_error


@respx.mock
def test_ollama_embedder_rejects_bad_payload():
    cfg = EmbeddingsConfig(enabled=True, ollama_url="http://ollama.test:11434")
    respx.post("http://ollama.test:11434/api/embed").respond(200, json={"embeddings": [[1.0]]})
    assert OllamaEmbedder(cfg)(["a", "b"]) is None
