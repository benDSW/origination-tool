"""Data contracts passed between layers.

Adapters translate external API payloads INTO these models; services and the
dashboard only ever see these models. No raw JSON crosses a layer boundary.
All datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Mutable(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- #
# Evertrace-facing contracts (produced by the Evertrace adapter)
# --------------------------------------------------------------------------- #


class SearchFilter(_Frozen):
    """One saved-search filter. ``values`` is already split (no ``a|b`` strings)."""

    key: str
    operator: str = "in"  # "in" | "eq"
    values: tuple[str, ...]


class SavedSearch(_Frozen):
    external_id: str  # e.g. "sch_..."
    title: str
    filters: tuple[SearchFilter, ...] = ()


class NewSearchSpec(_Frozen):
    """What the dashboard asks the SearchCatalog to create on Evertrace."""

    title: str = Field(min_length=1, max_length=120)
    filters: tuple[SearchFilter, ...]


class SearchVocabulary(_Frozen):
    """Closed vocabularies for building a search form."""

    statuses: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    genders: tuple[str, ...] = ()
    profile_tags: tuple[str, ...] = ()
    age_ranges: tuple[str, ...] = ()
    education_levels: tuple[str, ...] = ()
    customer_focus: tuple[str, ...] = ()
    score_min: int = 1
    score_max: int = 10


class CompanyRef(_Frozen):
    name: str
    website_domain: str | None = None  # bare domain, e.g. "acme.co.uk"
    registry_url: str | None = None  # e.g. Companies House URL


class Signal(_Frozen):
    """A founder signal from Evertrace, normalised."""

    external_id: str  # e.g. "sig_..."
    first_name: str
    last_name: str
    email: str | None = None
    linkedin_slug: str | None = None  # "jane-doe" (never a full URL)
    city: str | None = None
    country: str | None = None
    gender: str | None = None
    score: int | None = None
    signal_type: str | None = None
    discovered_at: datetime
    summary: str | None = None
    industries: tuple[str, ...] = ()
    company: CompanyRef | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


# --------------------------------------------------------------------------- #
# RocketReach-facing contracts
# --------------------------------------------------------------------------- #


class EmailSource(StrEnum):
    EVERTRACE = "evertrace"
    ROCKETREACH = "rocketreach"
    MANUAL = "manual"


class LookupOutcome(StrEnum):
    FOUND = "found"  # an email passed every guardrail
    NOT_FOUND = "not_found"  # provider had no usable email
    REJECTED = "rejected"  # provider returned data that failed a guardrail
    ERROR = "error"  # transport / provider error (retryable on a later run)


class EmailLookupResult(_Frozen):
    outcome: LookupOutcome
    email: str | None = None
    grade: str | None = None
    reason: str | None = None  # human-readable why (shown in dashboard)


# --------------------------------------------------------------------------- #
# Affinity-facing contracts
# --------------------------------------------------------------------------- #


class KnownEntity(_Frozen):
    """One opportunity on a watched Affinity list, flattened for matching."""

    list_name: str
    opportunity_name: str
    company_names: tuple[str, ...] = ()
    company_domains: tuple[str, ...] = ()
    person_names: tuple[str, ...] = ()


class KnownEntities(_Frozen):
    """Snapshot of everything DSW has already seen, taken once per run."""

    entities: tuple[KnownEntity, ...] = ()


class CrmMatch(_Frozen):
    """Why a founder is considered already known."""

    matched_on: str  # "company_name" | "company_domain" | "person_name"
    list_name: str
    opportunity_name: str

    def describe(self) -> str:
        return f"{self.list_name}: '{self.opportunity_name}' (matched on {self.matched_on.replace('_', ' ')})"


class InteractionDirection(StrEnum):
    OUTBOUND = "outbound"  # DSW -> founder
    INBOUND = "inbound"  # founder -> DSW


class EmailInteraction(_Frozen):
    direction: InteractionDirection
    occurred_at: datetime
    subject: str | None = None


# --------------------------------------------------------------------------- #
# Inbox-facing contracts
# --------------------------------------------------------------------------- #


class InboxEventKind(StrEnum):
    SENT = "sent"
    REPLIED = "replied"
    DECK_RECEIVED = "deck_received"  # not detectable via Affinity; reserved for Graph


class InboxEvent(_Frozen):
    kind: InboxEventKind
    occurred_at: datetime


# --------------------------------------------------------------------------- #
# Tool-owned state (persisted by the persistence layer)
# --------------------------------------------------------------------------- #


class LocationPolicy(StrEnum):
    UK_OUTSIDE_LONDON = "uk_outside_london"  # default: UK, excluding London / inside-M25
    LONDON_ALLOWED = "london_allowed"  # UK including London (e.g. female founders in London)


class OutreachStatus(StrEnum):
    DISCOVERED = "discovered"
    EXCLUDED = "excluded"
    NEEDS_EMAIL = "needs_email"
    DRAFTED = "drafted"
    SENT = "sent"
    REPLIED = "replied"
    DECK_RECEIVED = "deck_received"
    CLOSED = "closed"


class ExclusionReason(StrEnum):
    LOCATION = "location"
    IN_AFFINITY = "in_affinity"


class CloseReason(StrEnum):
    SKIPPED = "skipped"
    DECLINED = "declined"
    LAPSED = "lapsed"
    NOT_RAISING = "not_raising"


class EventSource(StrEnum):
    PIPELINE = "pipeline"  # the daily origination run
    AUTO_INBOX = "auto:inbox"  # detected via the InboxTracker
    MANUAL = "manual"  # a dashboard click


class RunTrigger(StrEnum):
    CRON = "cron"
    MANUAL = "manual"


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"  # finished, but some items/searches errored
    FAILED = "failed"
    SKIPPED = "skipped"  # another run held the lock


class SearchConfig(_Mutable):
    """Local configuration for one Evertrace saved search."""

    id: int | None = None
    external_id: str
    title: str
    enabled: bool = False
    location_policy: LocationPolicy = LocationPolicy.UK_OUTSIDE_LONDON
    lookback_days: int = Field(default=30, ge=1, le=730)
    watermark: datetime | None = None  # max discovered_at successfully processed
    last_synced_at: datetime | None = None


class FounderRecord(_Mutable):
    id: int | None = None
    evertrace_signal_id: str
    linkedin_slug: str | None = None
    first_name: str
    last_name: str
    city: str | None = None
    country: str | None = None
    gender: str | None = None
    score: int | None = None
    signal_type: str | None = None
    summary: str | None = None
    discovered_at: datetime
    company_name: str | None = None
    company_domain: str | None = None
    company_registry_url: str | None = None
    evertrace_email: str | None = None
    created_at: datetime

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def linkedin_url(self) -> str | None:
        return f"https://www.linkedin.com/in/{self.linkedin_slug}" if self.linkedin_slug else None


class OutreachRecord(_Mutable):
    """One founder's journey from discovery to deck. Exactly one per founder."""

    id: int | None = None
    founder_id: int
    status: OutreachStatus = OutreachStatus.DISCOVERED
    exclusion_reason: ExclusionReason | None = None
    exclusion_detail: str | None = None
    close_reason: CloseReason | None = None
    email: str | None = None
    email_source: EmailSource | None = None
    draft_subject: str | None = None
    draft_body: str | None = None
    template_version: int | None = None
    created_at: datetime
    updated_at: datetime
    drafted_at: datetime | None = None
    sent_at: datetime | None = None
    replied_at: datetime | None = None
    deck_received_at: datetime | None = None
    closed_at: datetime | None = None
    last_tracked_at: datetime | None = None


class OutreachView(_Frozen):
    """Read model for the dashboard: outreach joined with its founder."""

    outreach: OutreachRecord
    founder: FounderRecord
    search_titles: tuple[str, ...] = ()


class StatusEvent(_Frozen):
    outreach_id: int
    from_status: OutreachStatus
    to_status: OutreachStatus
    source: EventSource
    at: datetime
    note: str | None = None


class EmailLookupRecord(_Frozen):
    """Cache row: one provider lookup per founder, ever (unless outcome == ERROR)."""

    founder_id: int
    provider: EmailSource
    outcome: LookupOutcome
    email: str | None = None
    grade: str | None = None
    reason: str | None = None
    created_at: datetime


class EmailTemplate(_Frozen):
    subject: str
    body: str
    version: int = 1


class Draft(_Frozen):
    to: str
    subject: str
    body: str

    def as_text(self) -> str:
        """Whole draft as one copy-pasteable block."""
        return f"To: {self.to}\nSubject: {self.subject}\n\n{self.body}"


class RunCounts(_Mutable):
    searches: int = 0
    signals_seen: int = 0
    founders_new: int = 0
    excluded_location: int = 0
    excluded_affinity: int = 0
    drafted: int = 0
    needs_email: int = 0
    rocketreach_lookups: int = 0
    tracking_updates: int = 0
    errors: int = 0


class RunRecord(_Mutable):
    id: int | None = None
    trigger: RunTrigger
    state: RunState = RunState.RUNNING
    dry_run: bool = False
    started_at: datetime
    finished_at: datetime | None = None
    counts: RunCounts = Field(default_factory=RunCounts)
    errors: list[str] = Field(default_factory=list)  # short, secret-free messages
