"""Services: the only code that moves data between layers.

Services receive ports (``app.domain.ports``) by constructor injection and never
import an adapter, the database or the web framework (enforced by
``.importlinter``). :class:`Services` is the bundle the dashboard and the CLI
jobs receive from ``app.container``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.domain.models import EmailTemplate
from app.services.clock import Clock, utc_now
from app.services.errors import (
    ExternalServiceError,
    InvalidAction,
    NotFound,
    ServiceError,
    TemplateValidationError,
    ValidationFailed,
)
from app.services.origination import OriginationService
from app.services.outreach import (
    DashboardSummary,
    OutreachDetail,
    OutreachItem,
    OutreachService,
    PipelineBoard,
)
from app.services.searches import SearchService, city_filter
from app.services.sessions import SessionService
from app.services.suppression import SuppressionKeys
from app.services.templates import TemplateService
from app.services.tracking import TrackingResult, TrackingService

logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything the web layer and the jobs need, already wired."""

    origination: OriginationService
    tracking: TrackingService
    outreach: OutreachService
    templates: TemplateService
    searches: SearchService
    clock: Clock = utc_now
    # Server-side dashboard sessions. None means no login can succeed (fail closed).
    sessions: SessionService | None = None
    closers: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def save_template(self, subject: str, body: str) -> tuple[EmailTemplate, int]:
        """Save a new template version and re-render open drafts with it.

        Returns the template and how many drafts changed. Raises
        TemplateValidationError if the template is invalid (nothing is saved).
        """
        template = self.templates.save(subject, body)
        return template, self.outreach.rerender_open_drafts()

    def close(self) -> None:
        """Release HTTP clients and DB connections (app shutdown / end of a job)."""
        for closer in reversed(self.closers):
            try:
                closer()
            except Exception as exc:
                logger.warning("Error while closing a resource (%s)", type(exc).__name__)
        self.closers.clear()


__all__ = [
    "Clock",
    "DashboardSummary",
    "ExternalServiceError",
    "InvalidAction",
    "NotFound",
    "OriginationService",
    "OutreachDetail",
    "OutreachItem",
    "OutreachService",
    "PipelineBoard",
    "SearchService",
    "ServiceError",
    "Services",
    "SessionService",
    "SuppressionKeys",
    "TemplateService",
    "TemplateValidationError",
    "TrackingResult",
    "TrackingService",
    "ValidationFailed",
    "city_filter",
    "utc_now",
]
