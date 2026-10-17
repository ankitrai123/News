# Ollama and local models

Phase 1 can run without any model. The default model provider is NVIDIA's free hosted API
(`llm.provider: nvidia`). Ollama is the fully local alternative: nothing leaves your machine,
there are no rate limits or trial terms, and output can be shared subject only to each model's
licence. Switch with `llm.provider: ollama` and `clustering.embeddings.provider: ollama`.

## Install

* **macOS / Windows:** download the installer from <https://ollama.com/download>.
* **Linux:** `curl -fsSL https://ollama.com/install.sh | sh` (read the script first if you prefer),
  or follow the manual steps on the same page.

Ollama listens on `http://localhost:11434`. Check with `ollama list` or `intelpod doctor`.

## Enable embeddings for clustering (optional)

```bash
ollama pull nomic-embed-text      # ~270 MB, runs on CPU
```

Then set in `config/config.yaml`:

```yaml
clustering:
  embeddings:
    enabled: true
    provider: ollama
    model: nomic-embed-text
llm:
  provider: ollama
  tasks:
    event_review:
      models:
        - {id: "qwen2.5:7b"}      # any instruct model you have pulled
```

If Ollama is down or the model is missing, the cluster stage logs the reason, records
`"embeddings": "unavailable (...)"` in its output and continues with lexical similarity.
Embedding support was tested against a mocked Ollama API only; the build environment had no
Ollama install.

## Choosing models for later phases

`intelpod doctor` prints a recommendation from your RAM. Rough guide (quantized models):

| Machine | Mode | Suggested instruct models |
|---|---|---|
| < 12 GB RAM, no GPU | lightweight | 1–4B (e.g. `llama3.2:3b`, `qwen2.5:3b`, `gemma3:4b`) — slower and less precise; keep episodes shorter |
| 16–32 GB RAM or 8 GB+ GPU | standard | 7–9B (e.g. `qwen2.5:7b`, `llama3.1:8b`) |
| 32 GB+ RAM or 16 GB+ GPU | large | 14B+ (e.g. `qwen2.5:14b`) |

Model names and availability change; check `ollama.com/library`. Each model has its own licence.
Model choice, context size and concurrency will be configurable per pipeline task.
