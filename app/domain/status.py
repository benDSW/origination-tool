"""Outreach status state machine.

Every status change in the system goes through :func:`transition`, which
validates the move, stamps the relevant timestamp and returns the audit event.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.models import (
    CloseReason,
    EventSource,
    OutreachRecord,
    OutreachStatus,
    StatusEvent,
)

S = OutreachStatus

ALLOWED_TRANSITIONS: dict[OutreachStatus, frozenset[OutreachStatus]] = {
    S.DISCOVERED: frozenset({S.EXCLUDED, S.NEEDS_EMAIL, S.DRAFTED, S.CLOSED}),
    # An exclusion can be overridden ("draft anyway").
    S.EXCLUDED: frozenset({S.NEEDS_EMAIL, S.DRAFTED, S.CLOSED}),
    S.NEEDS_EMAIL: frozenset({S.DRAFTED, S.CLOSED}),
    # DRAFTED -> NEEDS_EMAIL lets the user discard a wrong address.
    # DRAFTED -> REPLIED covers a reply detected before the send was.
    S.DRAFTED: frozenset({S.SENT, S.REPLIED, S.NEEDS_EMAIL, S.CLOSED}),
    S.SENT: frozenset({S.REPLIED, S.DECK_RECEIVED, S.CLOSED}),
    S.REPLIED: frozenset({S.DECK_RECEIVED, S.CLOSED}),
    S.DECK_RECEIVED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
}

OPEN_STATUSES: frozenset[OutreachStatus] = frozenset(
    {S.DISCOVERED, S.EXCLUDED, S.NEEDS_EMAIL, S.DRAFTED, S.SENT, S.REPLIED}
)
TRACKABLE_STATUSES: frozenset[OutreachStatus] = frozenset({S.DRAFTED, S.SENT})


class InvalidTransition(ValueError):
    def __init__(self, current: OutreachStatus, target: OutreachStatus) -> None:
        super().__init__(f"Cannot move outreach from '{current}' to '{target}'")
        self.current = current
        self.target = target


def can_transition(current: OutreachStatus, target: OutreachStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def transition(
    outreach: OutreachRecord,
    target: OutreachStatus,
    *,
    source: EventSource,
    at: datetime,
    note: str | None = None,
    close_reason: CloseReason | None = None,
) -> StatusEvent:
    """Mutate ``outreach`` to ``target`` and return the audit event.

    Raises :class:`InvalidTransition` if the move is not allowed. The caller is
    responsible for persisting both the record and the event.
    """
    if outreach.id is None:
        raise ValueError("outreach must be persisted before it can transition")
    current = outreach.status
    if not can_transition(current, target):
        raise InvalidTransition(current, target)

    outreach.status = target
    outreach.updated_at = at
    if target is S.DRAFTED:
        outreach.drafted_at = at
        outreach.exclusion_reason = None
        outreach.exclusion_detail = None
    elif target is S.NEEDS_EMAIL:
        outreach.exclusion_reason = None
        outreach.exclusion_detail = None
        outreach.email = None
        outreach.email_source = None
        outreach.draft_subject = None
        outreach.draft_body = None
        outreach.drafted_at = None
    elif target is S.SENT:
        outreach.sent_at = outreach.sent_at or at
    elif target is S.REPLIED:
        outreach.replied_at = outreach.replied_at or at
    elif target is S.DECK_RECEIVED:
        outreach.deck_received_at = outreach.deck_received_at or at
    elif target is S.CLOSED:
        outreach.closed_at = at
        outreach.close_reason = close_reason or CloseReason.SKIPPED

    return StatusEvent(
        outreach_id=outreach.id,
        from_status=current,
        to_status=target,
        source=source,
        at=at,
        note=note,
    )
