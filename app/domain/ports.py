"""Ports: the interfaces between the application core and the outside world.

Integrations (Evertrace, RocketReach, Affinity, Inbox) and persistence implement
these. Services depend ONLY on these protocols, never on a concrete adapter, so
any layer can be replaced (e.g. Affinity-backed inbox tracking -> Microsoft
Graph) without touching orchestration code.

Error contract: adapters raise :class:`IntegrationError` (or a subclass) for any
failure talking to an external system. Messages must never contain secrets or
raw response bodies.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.domain.models import (
    EmailInteraction,
    EmailLookupRecord,
    EmailLookupResult,
    EmailSource,
    EmailTemplate,
    FounderRecord,
    InboxEvent,
    KnownEntities,
    NewSearchSpec,
    OutreachRecord,
    OutreachStatus,
    OutreachView,
    RunRecord,
    SavedSearch,
    SearchConfig,
    SearchFilter,
    SearchVocabulary,
    Signal,
    StatusEvent,
)


class IntegrationError(RuntimeError):
    """An external system failed. ``retryable`` hints whether a later run may succeed."""

    def __init__(self, system: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"[{system}] {message}")
        self.system = system
        self.retryable = retryable


class NotFoundError(LookupError):
    pass


class LookupReservation(StrEnum):
    """Result of :meth:`Store.reserve_email_lookup`."""

    RESERVED = "reserved"
    CAP_REACHED = "cap_reached"
    ALREADY_CLAIMED = "already_claimed"


# --------------------------------------------------------------------------- #
# Evertrace layer
# --------------------------------------------------------------------------- #


@runtime_checkable
class SearchCatalog(Protocol):
    """Saved searches on Evertrace."""

    def list_searches(self) -> list[SavedSearch]: ...

    def get_search(self, external_id: str) -> SavedSearch: ...

    def create_search(self, spec: NewSearchSpec) -> SavedSearch: ...

    def vocabulary(self) -> SearchVocabulary: ...

    def search_cities(self, query: str, limit: int = 20) -> list[str]: ...


@runtime_checkable
class SignalSource(Protocol):
    """Founder signals on Evertrace."""

    def iter_signals(self, filters: tuple[SearchFilter, ...], since: datetime) -> Iterator[Signal]:
        """Yield every signal matching ``filters`` discovered at or after ``since``.

        Handles pagination internally. Each yielded Signal is fully populated,
        including ``company`` (current founder role) and ``email`` when known.
        """
        ...


# --------------------------------------------------------------------------- #
# RocketReach layer
# --------------------------------------------------------------------------- #


@runtime_checkable
class EmailFinder(Protocol):
    def find_email(
        self,
        *,
        linkedin_slug: str,
        full_name: str,
        company_domain: str | None,
    ) -> EmailLookupResult:
        """Look a person up by LinkedIn profile ONLY and apply identity/quality guardrails.

        Must never raise for "no result" - return NOT_FOUND / REJECTED instead.
        Raises IntegrationError only for transport/auth failures.
        """
        ...

    def remaining_credits(self) -> int | None:
        """Remaining lookup credits, or None if unknown."""
        ...


# --------------------------------------------------------------------------- #
# Affinity layer (read-only)
# --------------------------------------------------------------------------- #


@runtime_checkable
class CrmGateway(Protocol):
    def known_entities(self) -> KnownEntities:
        """Snapshot of every opportunity on the watched lists (Origination + Applicant)."""
        ...

    def email_interactions(self, email: str, since: datetime) -> list[EmailInteraction]:
        """Email interactions between DSW and the person with this address since ``since``.

        Returns [] when the person is unknown to the CRM.
        """
        ...


# --------------------------------------------------------------------------- #
# Inbox layer
# --------------------------------------------------------------------------- #


@runtime_checkable
class InboxTracker(Protocol):
    def events_for(self, email: str, since: datetime) -> list[InboxEvent]:
        """Outreach events (sent / replied / deck) for this founder address since ``since``,
        oldest first."""
        ...


# --------------------------------------------------------------------------- #
# Persistence (tool-owned state)
# --------------------------------------------------------------------------- #


@runtime_checkable
class Store(Protocol):
    """Everything the tool remembers. Each method is its own transaction."""

    # Searches
    def list_search_configs(self) -> list[SearchConfig]: ...
    def get_search_config(self, search_id: int) -> SearchConfig: ...  # NotFoundError
    def upsert_search_from_catalog(self, saved: SavedSearch, *, synced_at: datetime) -> SearchConfig:
        """Insert a new search (disabled by default) or refresh the title of an existing one.
        Never overwrites local settings (enabled / policy / lookback / watermark)."""
        ...

    def save_search_config(self, config: SearchConfig) -> SearchConfig: ...

    # Founders
    def find_founder(self, *, evertrace_signal_id: str, linkedin_slug: str | None) -> FounderRecord | None:
        """Match on signal id OR linkedin slug (same person across searches/signal types)."""
        ...

    def add_founder(self, founder: FounderRecord) -> FounderRecord: ...
    def get_founder(self, founder_id: int) -> FounderRecord: ...  # NotFoundError
    def link_founder_to_search(self, founder_id: int, search_id: int) -> None: ...  # idempotent
    def delete_founder(self, founder_id: int) -> None:
        """Hard delete founder + outreach + events + lookups (right to erasure)."""
        ...

    # Outreach
    def add_outreach(self, outreach: OutreachRecord) -> OutreachRecord: ...
    def get_outreach(self, outreach_id: int) -> OutreachRecord: ...  # NotFoundError
    def get_outreach_for_founder(self, founder_id: int) -> OutreachRecord | None: ...
    def save_outreach(self, outreach: OutreachRecord, event: StatusEvent | None = None) -> OutreachRecord:
        """Persist the record and (optionally) its audit event atomically."""
        ...

    def list_outreach(
        self, statuses: frozenset[OutreachStatus] | None = None, *, limit: int = 500
    ) -> list[OutreachView]:
        """Newest first (by updated_at)."""
        ...

    def get_outreach_view(self, outreach_id: int) -> OutreachView: ...  # NotFoundError
    def count_by_status(self) -> dict[OutreachStatus, int]: ...
    def list_status_events(self, outreach_id: int) -> list[StatusEvent]: ...  # oldest first

    # Email lookup cache
    def get_email_lookup(self, founder_id: int, provider: EmailSource) -> EmailLookupRecord | None: ...
    def list_email_lookups(
        self, founder_ids: Sequence[int], provider: EmailSource
    ) -> dict[int, EmailLookupRecord]:
        """Cache rows for many founders at once, keyed by founder id (missing ids omitted)."""
        ...

    def add_email_lookup(self, record: EmailLookupRecord) -> None:
        """Insert or replace the single cache row for (founder, provider)."""
        ...

    def count_email_lookups_since(self, provider: EmailSource, since: datetime) -> int: ...

    # RocketReach budget (race-safe: the claim and the daily count are one transaction)
    def reserve_email_lookup(
        self, reservation: EmailLookupRecord, *, day_start: datetime, daily_cap: int
    ) -> LookupReservation:
        """Atomically claim this founder's lookup AND one unit of today's budget, BEFORE
        the paid call is made. CAP_REACHED writes nothing; ALREADY_CLAIMED means a
        non-error lookup already exists for (founder, provider)."""
        ...

    def lookups_reserved_since(self, provider: EmailSource, day_start: datetime) -> int: ...

    # Erasure (UK GDPR) - suppressions hold only opaque keyed hashes, never PII
    def erase_founder(self, founder_id: int, *, suppress: Sequence[str], at: datetime) -> None:
        """Hard-delete the founder and everything cascading, and record the given
        suppression hashes, in one transaction. NotFoundError if missing."""
        ...

    def is_suppressed(self, key_hashes: Sequence[str]) -> bool: ...

    # Runs + owned locks (only the holder can heartbeat or release)
    def acquire_owned_lock(self, name: str, *, owner: str, now: datetime, ttl_seconds: int) -> bool:
        """Atomically take a named lock; a stale lock (not refreshed within ttl) may be
        taken over."""
        ...

    def refresh_owned_lock(self, name: str, *, owner: str, now: datetime) -> bool:
        """Heartbeat. False means the lock was lost: the holder must stop working."""
        ...

    def release_owned_lock(self, name: str, *, owner: str) -> None:
        """Release only if ``owner`` still holds it."""
        ...

    def add_run(self, run: RunRecord) -> RunRecord: ...
    def save_run(self, run: RunRecord) -> RunRecord: ...
    def list_runs(self, limit: int = 50) -> list[RunRecord]: ...  # newest first
    def latest_run(self) -> RunRecord | None: ...

    # Template
    def get_template(self) -> EmailTemplate:
        """Current template; seeds DEFAULT_TEMPLATE on first call."""
        ...

    def save_template(self, subject: str, body: str) -> EmailTemplate:
        """Store a new version (version = previous + 1)."""
        ...

    # Dashboard sessions (server-side, so logout / password change revokes cookies)
    def add_admin_session(self, sid_hash: str, *, created_at: datetime, expires_at: datetime) -> None: ...
    def admin_session_active(self, sid_hash: str, *, now: datetime) -> bool: ...
    def delete_admin_session(self, sid_hash: str) -> None: ...
    def delete_all_admin_sessions(self) -> int: ...
