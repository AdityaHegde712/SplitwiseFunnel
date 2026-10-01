"""Verbose structured logging with sensitive-field redaction."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


LOGGER_NAME = "splitwise_funnel"
LOG_FILE_NAME = "splitwise_funnel.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
SENSITIVE_KEY_FRAGMENTS = (
    "email",
    "html",
    "mime",
    "attachment",
    "oauth",
    "token",
    "credential",
    "password",
    "cookie",
    "authorization",
    "participant_id",
    "payment_last_four",
)


class _WindowsSafeRotatingFileHandler(RotatingFileHandler):
    """Release file handles after each event so temporary log folders clean up."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        finally:
            if self.stream is not None:
                self.stream.close()
                self.stream = None


def configure_logging(log_directory: Path) -> logging.Logger:
    """Configure the application logger to write verbose local audit events."""
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / LOG_FILE_NAME
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    _remove_managed_handlers(logger)
    handler = _WindowsSafeRotatingFileHandler(
        log_path,
        encoding="utf-8",
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        delay=True,
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, "_splitwise_funnel_managed", True)
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Write one compact, redacted JSON event at DEBUG level."""
    _remove_unavailable_handlers_in_hierarchy(logger)
    event_fields = {
        key: _redact_value(key, value)
        for key, value in fields.items()
        if key not in {"timestamp", "level", "event"}
    }
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "DEBUG",
        "event": event,
        **event_fields,
    }
    logger.debug(json.dumps(payload, separators=(",", ":"), default=str))


def _remove_managed_handlers(logger: logging.Logger) -> None:
    """Replace only handlers this module owns, preserving host configuration."""
    for handler in tuple(logger.handlers):
        if not getattr(handler, "_splitwise_funnel_managed", False):
            continue
        logger.removeHandler(handler)
        handler.close()


def _remove_unavailable_managed_handlers(logger: logging.Logger) -> None:
    """Detach stale test or runtime sinks before logging can emit to stderr."""
    for handler in tuple(logger.handlers):
        is_managed_handler: bool = getattr(handler, "_splitwise_funnel_managed", False)
        handler_path: str | None = getattr(handler, "baseFilename", None)
        if not is_managed_handler or not isinstance(handler_path, str):
            continue
        if Path(handler_path).parent.is_dir():
            continue
        logger.removeHandler(handler)
        handler.close()


def _remove_unavailable_handlers_in_hierarchy(logger: logging.Logger) -> None:
    """Check ancestor sinks because component loggers propagate to the root sink."""
    current: logging.Logger | None = logger
    while current is not None:
        _remove_unavailable_managed_handlers(current)
        parent = current.parent
        current = parent if isinstance(parent, logging.Logger) else None


def _redact_value(key: str, value: Any) -> Any:
    """Redact sensitive values recursively so nested payloads stay safe."""
    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            nested_key: _redact_value(str(nested_key), nested_value)
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(key, item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized_key = key.lower()
    return any(fragment in normalized_key for fragment in SENSITIVE_KEY_FRAGMENTS)
