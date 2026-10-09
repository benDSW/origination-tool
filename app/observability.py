"""Logging setup with a secret-redacting filter.

Defence in depth: code should never log secrets in the first place, but if an
exception message or third-party library echoes an API key or an Authorization
header, this filter scrubs it before it reaches stdout (and Railway's logs).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

REDACTED = "***REDACTED***"

_AUTH_HEADER = re.compile(r"(?i)(authorization|api[-_]?key|x-api-key)(['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
        return _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.msg = self.redact(message)
        record.args = None
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


def configure_logging(secrets: Iterable[str] = (), level: int = logging.INFO) -> RedactingFilter:
    """Install a single redacting stream handler on the root logger."""
    redactor = RedactingFilter(secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(redactor)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep it at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return redactor
