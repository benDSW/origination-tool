"""Automatic sent / replied tracking (R15) through the ``InboxTracker`` port.

The tracker only ever moves outreach *forward* along the state machine, and only
from the states where the evidence makes sense. Manual dashboard buttons always
work as well; if the user already clicked "Sent", a later SENT event is simply
ignored.

The dashboard syncs on a timer, so to keep CRM API usage modest, outreach that
was sent (or drafted) more than ``RECENT_WINDOW`` ago is re-checked at most once
per ``STALE_RECHECK``. ``sync(force=True)`` (the "Check now" button) checks all.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.models import (
    EventSource,
    InboxEvent,
    InboxEventKind,
    OutreachRecord,
    OutreachStatus,
)
from app.domain.ports import InboxTracker, IntegrationError, Store
from app.domain.status import TRACKABLE_STATUSES, transition
from app.services.clock import Clock, utc_now

logger = logging.getLogger(__name__)

S = OutreachStatus

# Which inbox evidence may move which status. DECK_RECEIVED is not detectable via
# Affinity today but a Microsoft Graph tracker could emit it, so it is honoured.
_MOVES: dict[InboxEventKind, tuple[frozenset[OutreachStatus], OutreachStatus]] = {
    InboxEventKind.SENT: (frozenset({S.DRAFTED}), S.SENT),
    InboxEventKind.REPLIED: (frozenset({S.DRAFTED, S.SENT}), S.REPLIED),
    InboxEventKind.DECK_RECEIVED: (frozenset({S.SENT, S.REPLIED}), S.DECK_RECEIVED),
}

MAX_TRACKED = 10_000
RECENT_WINDOW = timedelta(days=21)
STALE_RECHECK = timedelta(hours=24)


@dataclass
class TrackingResult:
    """Outcome of one sync: how many status changes, and short secret-free errors."""

    updates: int = 0
    checked: int = 0
    deferred: int = 0  # older outreach checked recently enough to skip this time
    errors: list[str] = field(default_factory=list)


class TrackingService:
    def __init__(self, store: Store, inbox: InboxTracker, clock: Clock = utc_now) -> None:
        self._store = store
        self._inbox = inbox
        self._clock = clock

    def sync(self, *, force: bool = False) -> TrackingResult:
        """Check every DRAFTED / SENT outreach that has an address (older ones at
        most daily unless ``force``). One failing founder never stops the loop."""
        result = TrackingResult()
        now = self._clock()
        for view in self._store.list_outreach(TRACKABLE_STATUSES, limit=MAX_TRACKED):
            outreach = view.outreach
            if not outreach.email or outreach.id is None:
                continue
            if not force and _can_wait(outreach, now):
                result.deferred += 1
                continue
            result.checked += 1
            try:
                result.updates += self._track_one(outreach, outreach.email)
            except IntegrationError as exc:
                result.errors.append(f"Tracking outreach {outreach.id}: {exc}")
            except Exception as exc:
                logger.exception("Tracking outreach %s failed", outreach.id)
                result.errors.append(f"Tracking outreach {outreach.id}: unexpected {type(exc).__name__}")
        if result.updates or result.errors:
            logger.info("Tracking sync: %d updates, %d errors", result.updates, len(result.errors))
        return result

    def _track_one(self, outreach: OutreachRecord, email: str) -> int:
        since = outreach.drafted_at or outreach.created_at
        events = self._inbox.events_for(email, since=since)
        changes = self._apply(outreach, events, since=since)
        outreach.last_tracked_at = self._clock()
        self._store.save_outreach(outreach)
        return changes

    def _apply(self, outreach: OutreachRecord, events: Iterable[InboxEvent], *, since: datetime) -> int:
        changes = 0
        for event in sorted(events, key=lambda e: e.occurred_at):
            if event.occurred_at < since:
                continue  # defence in depth: the port promises this already
            move = _MOVES.get(event.kind)
            if move is None:
                continue
            allowed_from, target = move
            if outreach.status not in allowed_from:
                continue
            status_event = transition(
                outreach,
                target,
                source=EventSource.AUTO_INBOX,
                at=event.occurred_at,
                note=f"Detected {event.kind.value} email",
            )
            self._store.save_outreach(outreach, status_event)
            changes += 1
        return changes


def _can_wait(outreach: OutreachRecord, now: datetime) -> bool:
    """Old outreach that was already checked within ``STALE_RECHECK``."""
    started = outreach.sent_at or outreach.drafted_at or outreach.created_at
    last = outreach.last_tracked_at
    return last is not None and now - started > RECENT_WINDOW and now - last < STALE_RECHECK
