"""Structured logging with secret redaction."""
from __future__ import annotations

import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)$", re.I)
_SECRET_PARAM = re.compile(r"((?:api[_-]?key|apikey|token|access_token)=)[^&\s]+", re.I)


def _secret_values() -> list[str]:
    return [v for k, v in os.environ.items() if _SECRET_ENV.search(k) and v and len(v) >= 6]


class RedactingFilter(logging.Filter):
    """Removes API keys / tokens from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = _SECRET_PARAM.sub(r"\1***", msg)
        for secret in _secret_values():
            redacted = redacted.replace(secret, "***")
        if redacted != msg:
            record.msg, record.args = redacted, ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "stage", "provider"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def setup_logging(level: str = "INFO", log_dir: Path | None = None, json_logs: bool = False) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level.upper())
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    console.addFilter(RedactingFilter())
    root.addHandler(console)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "intelpod.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(JsonFormatter() if json_logs else logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        fh.addFilter(RedactingFilter())
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
