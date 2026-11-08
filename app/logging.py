import json
import logging
import os
import re
from datetime import datetime, timezone
from logging.config import dictConfig


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("job_id", "correlation_id", "stage", "duration_ms", "attempt", "worker_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
        message = payload["message"]
        for name in ("LLM_API_KEY", "LLM_BASE_URL", "DATABASE_URL"):
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", message)
        message = re.sub(r"(?i)(https?://)[^\s]+", "[URL REDACTED]", message)
        payload["message"] = message
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": JsonFormatter}},
        "handlers": {"console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
        }},
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            name: {"handlers": [], "propagate": True, "level": level}
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        },
    })
