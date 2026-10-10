"""Errors the services raise to their callers (the dashboard and the CLI jobs).

Every ``ServiceError.message`` is safe to show to the user: it never contains
secrets, raw API responses or stack traces. The web layer can render
``exc.message`` directly and choose an HTTP status from the subclass.
"""

from __future__ import annotations

from collections.abc import Sequence


class ServiceError(Exception):
    """Base class. ``message`` is user-safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(ServiceError):
    """The record the user asked for does not exist (any more). Web: 404."""


class InvalidAction(ServiceError):
    """The action is not allowed in the record's current state (e.g. a bad status move). Web: 409."""


class ValidationFailed(ServiceError):
    """User input was rejected. ``problems`` lists every reason. Web: 400 / 422."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: list[str] = list(problems) or ["Invalid input"]
        super().__init__("; ".join(self.problems))


class TemplateValidationError(ValidationFailed):
    """The email template has problems (unknown merge fields, empty subject, ...)."""


class ExternalServiceError(ServiceError):
    """An external system (Evertrace, Affinity, RocketReach) failed. Web: 502.

    Built from ``str(IntegrationError)``, which the port contract guarantees is
    secret-free.
    """
