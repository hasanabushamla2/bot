"""Structured logging configuration — R12: bounded file rotation.

All log messages are structured JSON for machine readability.
Secrets are automatically filtered — never print API keys in logs.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Any

import structlog


def _secrets_filter(_logger: Any, _method: Any, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove any key that looks like a secret from log output."""
    secret_keys = {
        "api_key",
        "api_secret",
        "secret",
        "password",
        "token",
        "private_key",
        "passphrase",
        "credential",
    }
    for key in list(event_dict):
        key_lower = key.lower()
        if any(sk in key_lower for sk in secret_keys):
            event_dict[key] = "***REDACTED***"
    return event_dict


def setup_logging(
    level: str = "INFO",
    fmt: str = "json",
    log_dir: str = "logs",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> None:
    """Configure structured logging for the entire application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        fmt: "json" for machine-readable or "text" for human-readable.
        log_dir: Directory for rotating log files.
        max_bytes: Max bytes per log file before rotation.
        backup_count: Number of rotated backups to keep.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Standard library logging compatibility — this sets up the root logger
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    # Ensure log directory exists before creating RotatingFileHandler
    os.makedirs(log_dir, exist_ok=True)

    # Now add the rotating file handler (after basicConfig, so it's not cleared)
    root = logging.getLogger()
    file_handler = logging.handlers.RotatingFileHandler(
        filename=f"{log_dir}/engine.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(file_handler)

    processors: list[Any] = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        _secrets_filter,
    ]

    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger for the given component name."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
