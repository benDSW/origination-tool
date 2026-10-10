"""Shared helpers for turning an outreach record into a DRAFTED one.

Used by the daily run (Evertrace / RocketReach emails) and by dashboard actions
(manual email, "draft anyway"), so a draft is always built the same way.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.models import (
    Draft,
    EmailSource,
    EmailTemplate,
    EventSource,
    FounderRecord,
    OutreachRecord,
    OutreachStatus,
    StatusEvent,
)
from app.domain.status import transition
from app.domain.templating import build_draft


def draft_outreach(
    outreach: OutreachRecord,
    founder: FounderRecord,
    email: str,
    *,
    email_source: EmailSource,
    template: EmailTemplate,
    source: EventSource,
    at: datetime,
    note: str | None = None,
) -> StatusEvent:
    """Render the draft and move ``outreach`` to DRAFTED; return the audit event.

    The draft is rendered *before* the transition, so an invalid address
    (``TemplateError``) or an invalid move (``InvalidTransition``) leaves the
    record untouched.
    """
    draft = build_draft(template, founder, email)
    event = transition(outreach, OutreachStatus.DRAFTED, source=source, at=at, note=note)
    write_draft(outreach, draft, template)
    outreach.email_source = email_source
    return event


def write_draft(outreach: OutreachRecord, draft: Draft, template: EmailTemplate) -> None:
    outreach.email = draft.to
    outreach.draft_subject = draft.subject
    outreach.draft_body = draft.body
    outreach.template_version = template.version


def current_draft(outreach: OutreachRecord) -> Draft | None:
    """The copy-and-paste block for a DRAFTED outreach (None when there is none)."""
    if outreach.email and outreach.draft_subject is not None and outreach.draft_body is not None:
        return Draft(to=outreach.email, subject=outreach.draft_subject, body=outreach.draft_body)
    return None
