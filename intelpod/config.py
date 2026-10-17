"""Configuration loading.

Order of precedence (later wins):
  1. Defaults defined in the pydantic models below.
  2. YAML file: $INTELPOD_CONFIG, else config/config.yaml, else config/config.example.yaml.
  3. Environment variables prefixed INTELPOD_ (see .env.example), e.g.
     INTELPOD_DB_PATH, INTELPOD_LOG_LEVEL, INTELPOD_GDELT_ENABLED.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AppConfig(BaseModel):
    data_dir: str = "data"
    db_path: str = "data/intelpod.db"
    log_level: str = "INFO"
    log_json: bool = False
    user_agent: str = "IntelPod/0.1 (personal news research)"


class HttpConfig(BaseModel):
    timeout_seconds: float = 20
    max_retries: int = 3
    retry_timeouts: bool = True                    # LLM calls set False: fail over instead of waiting again
    backoff_base_seconds: float = 2.0
    per_host_min_interval_seconds: float = 1.0
    respect_robots_txt: bool = True


class GdeltQuery(BaseModel):
    topic: str
    query: str


class RssProviderConfig(BaseModel):
    enabled: bool = True


class GdeltProviderConfig(BaseModel):
    enabled: bool = True
    min_interval_seconds: float = 6.0
    timespan: str = "1d"
    max_records: int = Field(75, ge=1, le=250)
    sort: str = "HybridRel"
    language_filter: str = "sourcelang:english"
    queries: list[GdeltQuery] = Field(default_factory=list)


class NewsdataQuery(BaseModel):
    topic: str
    q: str | None = Field(None, max_length=100)   # free plan: max 100 characters
    category: str | None = None                    # comma-separated, max 5
    country: str | None = None                     # comma-separated ISO-2 codes, max 5


class NewsdataProviderConfig(BaseModel):
    """NewsData.io free tier: 200 credits/day, 30 per 15 min, 10 articles per credit, ~12 h delay."""

    enabled: bool = True
    api_key_env: str = "NEWSDATA_API_KEY"
    base_url: str = "https://newsdata.io/api/1"
    endpoint: str = "latest"
    language: str = "en"
    size: int = Field(10, ge=1, le=50)             # free plan maximum is 10
    search_field: str = Field("q", pattern="^(q|qInMeta)$")
    removeduplicate: bool = True
    prioritydomain: str | None = None              # top | medium | low
    max_pages_per_query: int = Field(1, ge=1, le=10)
    max_credits_per_run: int = Field(20, ge=1)     # stays under the 30-per-15-minute limit
    daily_credit_budget: int = Field(180, ge=1)    # leave headroom below the 200/day quota
    min_interval_seconds: float = 2.0
    queries: list[NewsdataQuery] = Field(default_factory=lambda: [q.model_copy() for q in DEFAULT_NEWSDATA_QUERIES])


DEFAULT_NEWSDATA_QUERIES = [
    NewsdataQuery(topic="geopolitics", q='sanctions OR ceasefire OR invasion OR "peace talks"'),
    NewsdataQuery(topic="trade_policy", q='tariff OR tariffs OR "export controls" OR "trade deal"'),
    NewsdataQuery(topic="markets_monetary", q='"central bank" OR "interest rate" OR inflation', category="business"),
    NewsdataQuery(topic="energy_commodities", q='OPEC OR "oil prices" OR LNG OR "natural gas"'),
    NewsdataQuery(topic="semiconductors_minerals", q='semiconductor OR chipmaker OR "rare earth" OR "critical minerals"'),
    NewsdataQuery(topic="technology_ai", q='"artificial intelligence" OR "AI regulation"', category="technology"),
    NewsdataQuery(topic="india", category="business", country="in"),
    NewsdataQuery(topic="india", category="politics", country="in"),
    NewsdataQuery(topic="geopolitics", category="world"),
]


class ProvidersConfig(BaseModel):
    rss: RssProviderConfig = Field(default_factory=RssProviderConfig)
    gdelt: GdeltProviderConfig = Field(default_factory=GdeltProviderConfig)
    newsdata: NewsdataProviderConfig = Field(default_factory=NewsdataProviderConfig)


class IngestionConfig(BaseModel):
    lookback_hours: int = 36
    feeds_file: str = "config/feeds.yaml"
    max_description_chars: int = 400
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)


class EmbeddingsConfig(BaseModel):
    enabled: bool = False
    provider: str = Field("nvidia", pattern="^(nvidia|ollama)$")
    ollama_url: str = "http://localhost:11434"
    model: str = "nomic-embed-text"                 # Ollama model
    nvidia_model: str = "nvidia/nemotron-3-embed-1b"  # NVIDIA API model (uses llm.providers.nvidia)
    nvidia_fallback_models: list[str] = Field(default_factory=lambda: ["nvidia/nv-embedqa-e5-v5"])
    input_type: str = "passage"                     # NVIDIA retrieval models require it
    truncate: str = "END"                           # NVIDIA: NONE | START | END
    batch_size: int = Field(50, ge=1, le=512)
    mode: str = Field("boost", pattern="^(boost|blend)$")  # boost: embeddings can only raise similarity
    weight: float = Field(0.5, ge=0, le=1)
    timeout_seconds: float = 60


class ClusteringConfig(BaseModel):
    similarity_threshold: float = 0.30
    member_min_similarity: float = 0.25
    merge_threshold: float = 0.42
    entity_weight: float = Field(0.30, ge=0, le=1)
    max_time_gap_hours: float = 72
    link_lookback_days: int = 7
    link_threshold: float = 0.38
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)


class RankingWeights(BaseModel):
    geopolitical: float = 1.0
    economic: float = 1.0
    business: float = 0.8
    india: float = 0.7
    novelty: float = 0.6
    long_term: float = 0.8
    corroboration: float = 0.9
    uncertainty: float = 0.3


class RankingConfig(BaseModel):
    weights: RankingWeights = Field(default_factory=RankingWeights)
    min_events: int = 5
    max_events: int = 8
    min_score: float = 30
    quiet_day_floor: float = 22
    max_per_topic: int = 3
    single_source_penalty: float = 0.85
    soft_news_penalty: float = 0.3
    corroboration_cap: int = 8


class LLMProviderConfig(BaseModel):
    base_url: str
    api_key_env: str | None = None                 # env var holding the key; None for local Ollama
    requests_per_minute: float = 0                 # client-side pacing; 0 = unlimited
    structured_output: str = Field("none", pattern="^(none|guided_json|json_schema|json_object)$")
    extra_body: dict = Field(default_factory=dict)  # provider-specific request fields
    poll_url: str | None = None                     # NVIDIA: where 202-queued requests are polled
    backoff_base_seconds: float = 2.0               # retry backoff for 429/5xx (NVIDIA sends no Retry-After)


class LLMModelChoice(BaseModel):
    id: str
    extra_body: dict = Field(default_factory=dict)  # model-specific fields, e.g. reasoning switches
    max_tokens: int | None = None                   # override (reasoning models need more room)


class LLMTaskConfig(BaseModel):
    """Ordered model preference list: on 404/410 (not available / retired) the next model is tried."""

    models: list[LLMModelChoice] = Field(min_length=1)
    temperature: float = 0.1
    top_p: float | None = None                      # change temperature OR top_p, not both
    max_tokens: int = 900

    @model_validator(mode="before")
    @classmethod
    def _accept_single_model(cls, data):
        # Backwards compatible: `model: "x"` (+ optional `extra_body`) means a one-item list.
        if isinstance(data, dict) and "model" in data and "models" not in data:
            data = dict(data)
            data["models"] = [{"id": data.pop("model"), "extra_body": data.pop("extra_body", {}) or {}}]
        return data

    @property
    def model(self) -> str:
        return self.models[0].id


def _default_llm_providers() -> dict[str, LLMProviderConfig]:
    return {
        "nvidia": LLMProviderConfig(base_url="https://integrate.api.nvidia.com/v1", api_key_env="NVIDIA_API_KEY",
                                    requests_per_minute=20, structured_output="json_schema",
                                    poll_url="https://api.nvcf.nvidia.com/v2/nvcf/pexec/status",
                                    backoff_base_seconds=5.0),
        "ollama": LLMProviderConfig(base_url="http://localhost:11434/v1", api_key_env=None,
                                    requests_per_minute=0, structured_output="json_object"),
    }


_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
# Free NVIDIA endpoints reported live in Oct 2026 (third-party evidence; NVIDIA retires models often).
# Verify with `intelpod llm models` / `intelpod llm test` and reorder or replace as needed.
DEFAULT_REVIEW_MODELS = [
    LLMModelChoice(id="nvidia/nemotron-3.5-lightning-30b-a3b", extra_body=_NO_THINK),
    LLMModelChoice(id="openai/gpt-oss-20b", extra_body={"reasoning_effort": "low"}, max_tokens=2000),
    LLMModelChoice(id="google/gemma-4-31b-it", extra_body=_NO_THINK),
    LLMModelChoice(id="nvidia/nemotron-3-ultra-550b-a55b", extra_body=_NO_THINK),
]


class EventReviewConfig(BaseModel):
    enabled: bool = True
    max_events: int = Field(12, ge=0)               # first pass: highest-ranked events reviewed
    max_extra_reviews: int = Field(6, ge=0)         # follow-up reviews for events promoted into the briefing
    weight: float = Field(0.5, ge=0, le=1)          # share of each blended score taken from the model


class LLMConfig(BaseModel):
    enabled: bool = True
    provider: str = "nvidia"
    providers: dict[str, LLMProviderConfig] = Field(default_factory=_default_llm_providers)
    timeout_seconds: float = 120
    max_retries: int = 3
    cache: bool = True
    tasks: dict[str, LLMTaskConfig] = Field(default_factory=lambda: {
        "event_review": LLMTaskConfig(temperature=0.2, max_tokens=900, models=DEFAULT_REVIEW_MODELS)})
    event_review: EventReviewConfig = Field(default_factory=EventReviewConfig)


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    clustering: ClusteringConfig = Field(default_factory=ClusteringConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    taxonomy_file: str = "config/taxonomy.yaml"

    def resolve(self, path: str) -> Path:
        """Resolve a configured path relative to the project root."""
        p = Path(path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def db_file(self) -> Path:
        return self.resolve(self.app.db_path)

    @property
    def data_path(self) -> Path:
        return self.resolve(self.app.data_dir)


_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "INTELPOD_DATA_DIR": ("app", "data_dir"),
    "INTELPOD_DB_PATH": ("app", "db_path"),
    "INTELPOD_LOG_LEVEL": ("app", "log_level"),
    "INTELPOD_LOG_JSON": ("app", "log_json"),
    "INTELPOD_USER_AGENT": ("app", "user_agent"),
    "INTELPOD_LOOKBACK_HOURS": ("ingestion", "lookback_hours"),
    "INTELPOD_FEEDS_FILE": ("ingestion", "feeds_file"),
    "INTELPOD_RSS_ENABLED": ("ingestion", "providers", "rss", "enabled"),
    "INTELPOD_GDELT_ENABLED": ("ingestion", "providers", "gdelt", "enabled"),
    "INTELPOD_NEWSDATA_ENABLED": ("ingestion", "providers", "newsdata", "enabled"),
    "INTELPOD_LLM_ENABLED": ("llm", "enabled"),
    "INTELPOD_LLM_PROVIDER": ("llm", "provider"),
    "INTELPOD_EMBEDDINGS_ENABLED": ("clustering", "embeddings", "enabled"),
    "INTELPOD_EMBEDDINGS_PROVIDER": ("clustering", "embeddings", "provider"),
    "INTELPOD_OLLAMA_URL": ("clustering", "embeddings", "ollama_url"),
    "INTELPOD_EMBEDDING_MODEL": ("clustering", "embeddings", "model"),
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (no dependency). Existing env vars win.
    INTELPOD_NO_DOTENV=1 disables it (the test suite sets this so real keys are never used)."""
    if os.environ.get("INTELPOD_NO_DOTENV") == "1" or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _set_path(data: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


def _config_file() -> Path | None:
    explicit = os.environ.get("INTELPOD_CONFIG")
    if explicit:
        return Path(explicit)
    for name in ("config/config.yaml", "config/config.example.yaml"):
        p = PROJECT_ROOT / name
        if p.exists():
            return p
    return None


def load_settings(config_path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    path = Path(config_path) if config_path else _config_file()
    data: dict[str, Any] = {}
    if path and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for env, keys in _ENV_OVERRIDES.items():
        if env in os.environ:
            raw = os.environ[env]
            value: Any = raw
            if raw.lower() in {"true", "false"}:
                value = raw.lower() == "true"
            _set_path(data, keys, value)
    for dotted, value in (overrides or {}).items():
        _set_path(data, tuple(dotted.split(".")), value)
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
