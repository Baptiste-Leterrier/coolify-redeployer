"""Logging: one line per event on stdout (Coolify Runtime Logs), text or JSON.

Every record can carry structured fields through `extra={"fields": {...}}`. Records that
carry a `run_id` are also captured into the history database, so the log of each run
can be read back later (GET /runs/<id>/logs or `redeployer logs <id>`).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime

LOGGER_NAME = "redeployer"


class RedactFilter(logging.Filter):
    """Replaces any configured secret that would appear in a log line."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if len(s) >= 6]

    def _clean(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            record.msg = self._clean(record.getMessage())
            record.args = None
            fields = getattr(record, "fields", None)
            if fields:
                record.fields = {k: self._clean(v) if isinstance(v, str) else v for k, v in fields.items()}
        return True


def _fields(record: logging.LogRecord) -> dict:
    out = {}
    run_id = getattr(record, "run_id", None)
    if run_id is not None:
        out["run"] = run_id
    out.update(getattr(record, "fields", None) or {})
    return out


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")
        parts = [ts, f"{record.levelname:<7}", record.getMessage()]
        extra = _fields(record)
        if extra:
            parts.append(" ".join(f"{k}={_quote(v)}" for k, v in extra.items()))
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _quote(v) -> str:
    s = "" if v is None else str(v)
    return json.dumps(s) if (not s or any(c in s for c in ' "=\n\t')) else s


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "time": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "msg": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False, default=str)


class RunCaptureHandler(logging.Handler):
    """Sends records tagged with a run_id to a sink (the history store)."""

    def __init__(self, sink):
        super().__init__(level=logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        run_id = getattr(record, "run_id", None)
        if run_id is None:
            return
        try:
            self.sink(run_id, record.created, record.levelname.lower(), record.getMessage(),
                      getattr(record, "fields", None) or {})
        except Exception:  # never let log capture break a run
            pass


def setup(level: str, fmt: str, secrets: list[str]) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(level)
    logger.propagate = False
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    logger.addHandler(handler)
    logger.addFilter(RedactFilter(secrets))
    return logger


def attach_run_capture(logger: logging.Logger, sink) -> None:
    logger.addHandler(RunCaptureHandler(sink))


class RunLogger(logging.LoggerAdapter):
    """Adapter that tags every record with the run id and optional fields."""

    def process(self, msg, kwargs):
        fields = kwargs.pop("fields", None) or {}
        extra = kwargs.setdefault("extra", {})
        extra["run_id"] = self.extra.get("run_id")
        extra["fields"] = fields
        return msg, kwargs


def get(run_id=None) -> RunLogger:
    return RunLogger(logging.getLogger(LOGGER_NAME), {"run_id": run_id})
