"""Rotating JSON-line logs with redaction on both file and console handlers."""

import json
import logging
import math
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from errors import LoggingUnavailableError

LOGGER_NAME = "trading_agent"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
REDACTED = "[REDACTED]"
CONTEXT_FIELDS = ("symbol", "decision_id", "trade_id", "order_id", "run_id")
_SENSITIVE_KEY = re.compile(r"api.?key|secret|password|passwd|token|authorization|signature|cookie|credential", re.I)
_ASSIGNMENT = re.compile(
    r'''(?i)([\w-]*(?:api[_-]?key|secret|password|passwd|token|authorization|signature|cookie|credential)[\w-]*["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&}\]]+)'''
)
_AUTH = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;\"']+")
_URL_AUTH = re.compile(r"(https?://)[^/\s@]+@", re.I)
_OPAQUE_TOKEN = re.compile(r"\b[A-Za-z0-9_+/=]{32,}\b")


class Redactor:
    def __init__(self, secrets=()):
        self._secrets = tuple(sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True))

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        value = _AUTH.sub(lambda m: m[1] + " " + REDACTED, value)
        value = _ASSIGNMENT.sub(lambda m: m[1] + REDACTED, value)
        value = _URL_AUTH.sub(lambda m: m[1] + REDACTED + "@", value)
        return _OPAQUE_TOKEN.sub(REDACTED, value)

    def clean(self, value):
        if isinstance(value, str):
            return self.text(value)
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float:
            return value if math.isfinite(value) else "[NONFINITE]"
        if isinstance(value, dict):
            return {self.text(key): REDACTED if _SENSITIVE_KEY.search(key) else self.clean(item)
                    for key, item in value.items() if isinstance(key, str)}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        # Do not invoke arbitrary repr/str methods, which may disclose secrets.
        return "[UNSUPPORTED]"


class JsonFormatter(logging.Formatter):
    def __init__(self, *, secrets=(), run_id=None):
        super().__init__()
        self.redactor = Redactor(secrets)
        self.run_id = run_id

    def format(self, record):
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            # Format strings only after scrubbing structured arguments.
            message = record.msg if isinstance(record.msg, str) else self.redactor.clean(record.msg)
            if record.args and isinstance(message, str):
                args = self.redactor.clean(record.args)
                message = message % (tuple(args) if isinstance(args, list) else args)
            payload = {
                "timestamp": timestamp,
                "severity": record.levelname,
                "event_type": getattr(record, "event_type", "application"),
                "message": message,
                **{name: getattr(record, name, None) for name in CONTEXT_FIELDS},
            }
            if payload["run_id"] is None:
                payload["run_id"] = self.run_id
            if hasattr(record, "context"):
                payload["context"] = record.context
            if record.exc_info and record.exc_info[1] is not None:
                # No raw traceback/source lines or exception object repr.
                payload["exception"] = {"type": type(record.exc_info[1]).__name__,
                                        "message": str(record.exc_info[1])}
            return json.dumps(self.redactor.clean(payload), ensure_ascii=False, allow_nan=False)
        except Exception:
            # Do not include record, args, or the formatter's exception here.
            return json.dumps({"timestamp": timestamp, "severity": "ERROR",
                               "event_type": "logging_format_failure", "message": "Log record omitted"})


class _SafeHandler:
    def handleError(self, record):
        # logging's default handler can dump unsanitized msg/args to stderr.
        try:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            sys.stderr.write(json.dumps({"timestamp": timestamp, "severity": "ERROR",
                                        "event_type": "logging_write_failure", "message": "Log output unavailable"}) + "\n")
        except Exception:
            pass


class SafeRotatingFileHandler(_SafeHandler, RotatingFileHandler):
    pass


class SafeStreamHandler(_SafeHandler, logging.StreamHandler):
    pass


def configure_logging(log_directory: Path, *, secrets=(), run_id=None, console=True,
                      max_bytes=LOG_MAX_BYTES, backup_count=LOG_BACKUP_COUNT) -> logging.Logger:
    """Initialize only our logger; errors never echo a path or credential value."""
    if type(max_bytes) is not int or max_bytes <= 0 or type(backup_count) is not int or backup_count < 1:
        raise LoggingUnavailableError("Log rotation settings must be positive integers")
    try:
        directory = Path(log_directory)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = SafeRotatingFileHandler(directory / "bot.jsonl", maxBytes=max_bytes,
                                               backupCount=backup_count, encoding="utf-8")
    except (OSError, ValueError, TypeError):
        raise LoggingUnavailableError("Persistent logging could not be initialized") from None
    formatter = JsonFormatter(secrets=secrets, run_id=run_id)
    handlers = [file_handler]
    if console:
        handlers.append(SafeStreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
    logger = logging.getLogger(LOGGER_NAME)
    for previous in logger.handlers[:]:
        logger.removeHandler(previous)
        previous.close()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in handlers:
        logger.addHandler(handler)
    return logger
