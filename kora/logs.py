"""Structured JSON logging with correlation ids (request id / job id)."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid:
            payload["cid"] = cid
        for key in ("job_id", "job_kind", "plan_id", "file_id", "sha256", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(level)
    for noisy in ("sqlalchemy.engine", "httpx", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
