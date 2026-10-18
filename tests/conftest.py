from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Offline tests must never see real credentials (from the shell or from .env). The real
# values are kept aside only for the opt-in live tests (`pytest -m live`).
_KEY_NAMES = ("NEWSDATA_API_KEY", "NVIDIA_API_KEY", "GUARDIAN_API_KEY")


def _dotenv_values() -> dict:
    p = Path(__file__).resolve().parent.parent / ".env"
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


_dotenv = _dotenv_values()
LIVE_KEYS = {k: v for k in _KEY_NAMES if (v := os.environ.pop(k, None) or _dotenv.get(k))}
os.environ["INTELPOD_NO_DOTENV"] = "1"

sys.path.insert(0, str(Path(__file__).parent))

from fixtures import synthetic_news as sn  # noqa: E402
from intelpod.config import PROJECT_ROOT, load_settings  # noqa: E402
from intelpod.textutil import load_taxonomy  # noqa: E402


@pytest.fixture
def taxonomy():
    return load_taxonomy(PROJECT_ROOT / "config" / "taxonomy.yaml")


@pytest.fixture
def settings(tmp_path):
    feeds = tmp_path / "feeds.yaml"
    feeds.write_text(sn.feeds_yaml())
    return load_settings(PROJECT_ROOT / "config" / "config.example.yaml", overrides={
        "app.data_dir": str(tmp_path / "data"),
        "app.db_path": str(tmp_path / "data" / "test.db"),
        "ingestion.feeds_file": str(feeds),
        "ingestion.lookback_hours": 36,
        "http.max_retries": 1,
        "http.backoff_base_seconds": 0,
        "http.per_host_min_interval_seconds": 0,
        "http.respect_robots_txt": False,
        "ingestion.providers.gdelt.min_interval_seconds": 0,
        "ingestion.providers.gdelt.queries": [{"topic": "trade_policy", "query": "(tariff OR \"export controls\")"}],
        "research.feeds.feeds_file": str(tmp_path / "research_feeds.yaml"),  # none unless a test writes one
        "research.search_cache_hours": 0,
    })


@pytest.fixture
def live_key(monkeypatch):
    """Restore one real API key for a live test, or skip if it isn't configured."""
    def _get(name: str) -> str:
        if name not in LIVE_KEYS:
            pytest.skip(f"{name} not set (live test)")
        monkeypatch.setenv(name, LIVE_KEYS[name])
        return LIVE_KEYS[name]
    return _get
