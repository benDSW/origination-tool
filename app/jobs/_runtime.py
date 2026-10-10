"""Shared start-up for CLI jobs: settings, redacting logs, and the service graph."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from app.config import Settings, check_runtime_safety, describe_security_mode, get_settings
from app.container import build_services
from app.observability import configure_logging
from app.services import Services

logger = logging.getLogger(__name__)


def start() -> tuple[Settings, Services]:
    """Load settings, install the secret-redacting log handler, then build services.

    Logging is configured *before* any adapter exists, so nothing can log a key
    unredacted.
    """
    settings = get_settings()
    configure_logging(settings.secret_values())
    check_runtime_safety(settings)  # e.g. no ephemeral SQLite on the platform
    logger.info("Job security mode: %s", describe_security_mode(settings))
    return settings, build_services(settings)


def emit(payload: dict[str, Any]) -> None:
    """Print one JSON document to stdout (Railway shows it in the cron log)."""
    sys.stdout.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
    sys.stdout.flush()
