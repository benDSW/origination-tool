"""The editable email template (R12, R14): validation, versioned saves, live preview."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.models import Draft, EmailTemplate, FounderRecord
from app.domain.ports import Store
from app.domain.templating import build_draft, validate_template
from app.services.clock import Clock, utc_now
from app.services.errors import TemplateValidationError

# Synthetic sample used for previews only; never a real person.
SAMPLE_FOUNDER = FounderRecord(
    id=0,
    evertrace_signal_id="sig_preview",
    linkedin_slug="sam-sample",
    first_name="Sam",
    last_name="Sample",
    city="Leeds",
    country="United Kingdom",
    company_name="Sample Robotics",
    discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)
SAMPLE_EMAIL = "sam@sample-robotics.example"


class TemplateService:
    def __init__(self, store: Store, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    def current(self) -> EmailTemplate:
        return self._store.get_template()

    def save(self, subject: str, body: str) -> EmailTemplate:
        """Store a new template version. Raises TemplateValidationError (R12).

        Call ``OutreachService.rerender_open_drafts()`` afterwards (or use
        ``Services.save_template``) so open drafts pick up the change.
        """
        subject, body = _normalise(subject, body)
        problems = validate_template(subject, body)
        if problems:
            raise TemplateValidationError(problems)
        return self._store.save_template(subject, body)

    def preview(self, subject: str, body: str) -> Draft:
        """Render unsaved text for a synthetic sample founder (live preview)."""
        subject, body = _normalise(subject, body)
        problems = validate_template(subject, body)
        if problems:
            raise TemplateValidationError(problems)
        return build_draft(EmailTemplate(subject=subject, body=body, version=0), SAMPLE_FOUNDER, SAMPLE_EMAIL)


def _normalise(subject: str, body: str) -> tuple[str, str]:
    # Browsers submit textarea content with CRLF line endings.
    return (subject or "").strip(), (body or "").replace("\r\n", "\n").replace("\r", "\n")
