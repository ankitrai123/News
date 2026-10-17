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
from pydantic import BaseModel, Field

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


class ProvidersConfig(BaseModel):
    rss: RssProviderConfig = Field(default_factory=RssProviderConfig)
    gdelt: GdeltProviderConfig = Field(default_factory=GdeltProviderConfig)


class IngestionConfig(BaseModel):
    lookback_hours: int = 36
    feeds_file: str = "config/feeds.yaml"
    max_description_chars: int = 400
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)


class EmbeddingsConfig(BaseModel):
    enabled: bool = False
    ollama_url: str = "http://localhost:11434"
    model: str = "nomic-embed-text"
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


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    clustering: ClusteringConfig = Field(default_factory=ClusteringConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)
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
    "INTELPOD_EMBEDDINGS_ENABLED": ("clustering", "embeddings", "enabled"),
    "INTELPOD_OLLAMA_URL": ("clustering", "embeddings", "ollama_url"),
    "INTELPOD_EMBEDDING_MODEL": ("clustering", "embeddings", "model"),
}


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader (no dependency). Existing env vars win."""
    if not path.exists():
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
