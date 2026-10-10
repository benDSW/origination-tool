"""Derive outreach events from CRM email interactions.

Rules (see the plan's "Tracking" section):
- SENT    = the earliest OUTBOUND (DSW -> founder) email.
- REPLIED = the earliest INBOUND (founder -> DSW) email strictly after that first
  outbound email. If there is no outbound email at all, any inbound email counts:
  Ben may have sent the outreach from a mailbox Affinity does not sync.

DECK_RECEIVED is never emitted here: Affinity's interaction API exposes no
attachments, so decks are marked manually for now. A future ``GraphInboxTracker``
(Microsoft Graph) can see attachments and will emit it.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.models import EmailInteraction, InboxEvent, InboxEventKind, InteractionDirection
from app.domain.ports import CrmGateway


class AffinityInboxTracker:
    """:class:`~app.domain.ports.InboxTracker` backed by any :class:`CrmGateway`."""

    def __init__(self, crm: CrmGateway) -> None:
        self._crm = crm

    def events_for(self, email: str, since: datetime) -> list[InboxEvent]:
        """Oldest-first SENT/REPLIED events. CRM ``IntegrationError`` propagates to the caller."""
        interactions = sorted(
            (i for i in self._crm.email_interactions(email, since) if i.occurred_at >= since),
            key=lambda i: i.occurred_at,
        )
        first_out = _first(interactions, InteractionDirection.OUTBOUND)
        events: list[InboxEvent] = []
        if first_out is not None:
            events.append(InboxEvent(kind=InboxEventKind.SENT, occurred_at=first_out.occurred_at))
        reply = _first(
            interactions,
            InteractionDirection.INBOUND,
            after=first_out.occurred_at if first_out is not None else None,
        )
        if reply is not None:
            events.append(InboxEvent(kind=InboxEventKind.REPLIED, occurred_at=reply.occurred_at))
        return sorted(events, key=lambda e: e.occurred_at)


def _first(
    interactions: list[EmailInteraction],
    direction: InteractionDirection,
    *,
    after: datetime | None = None,
) -> EmailInteraction | None:
    """Earliest interaction in ``direction`` (strictly after ``after`` when given); input is sorted."""
    for interaction in interactions:
        if interaction.direction is direction and (after is None or interaction.occurred_at > after):
            return interaction
    return None
