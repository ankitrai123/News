from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    })
