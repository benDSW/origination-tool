"""SQLAlchemy ORM models (tables). Never returned outside the persistence package:
:mod:`app.persistence.repositories` maps every row to a domain DTO.

Portability notes (SQLite in dev/tests, Postgres in production):
- Enums are stored as plain VARCHAR(32) values (no native PG enum types), so
  adding a status never needs a type migration.
- Datetimes are stored as naive UTC and come back tz-aware (SQLite drops tzinfo).
- Constraint names follow a fixed naming convention so Alembic migrations are
  deterministic on both backends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.domain.models import (
    CloseReason,
    EmailSource,
    EventSource,
    ExclusionReason,
    LocationPolicy,
    LookupOutcome,
    OutreachStatus,
    RunState,
    RunTrigger,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC; always returns tz-aware UTC.

    Naive datetimes are rejected on the way in: on Windows ``datetime.now()`` is
    local time, and silently treating it as UTC would corrupt watermarks.
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime given to the database; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _enum(enum_cls: type[StrEnum]) -> Enum:
    """Store the enum's *value* (e.g. "needs_email") as VARCHAR(32)."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: dict[Any, Any] = {datetime: UTCDateTime()}


class SearchRow(Base):
    __tablename__ = "searches"
    __table_args__ = (UniqueConstraint("external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean)
    location_policy: Mapped[LocationPolicy] = mapped_column(_enum(LocationPolicy))
    lookback_days: Mapped[int] = mapped_column(Integer)
    watermark: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class FounderRow(Base):
    __tablename__ = "founders"
    __table_args__ = (
        UniqueConstraint("evertrace_signal_id"),
        UniqueConstraint("linkedin_slug"),  # NULLs never collide on SQLite or Postgres
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evertrace_signal_id: Mapped[str] = mapped_column(String(128))
    linkedin_slug: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(Text)
    last_name: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    gender: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(Integer)
    signal_type: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime())
    company_name: Mapped[str | None] = mapped_column(Text)
    company_domain: Mapped[str | None] = mapped_column(Text)
    company_registry_url: Mapped[str | None] = mapped_column(Text)
    evertrace_email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class FounderSearchRow(Base):
    __tablename__ = "founder_searches"
    __table_args__ = (UniqueConstraint("founder_id", "search_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    founder_id: Mapped[int] = mapped_column(ForeignKey("founders.id", ondelete="CASCADE"))
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"), index=True)


class OutreachRow(Base):
    __tablename__ = "outreach"
    __table_args__ = (
        UniqueConstraint("founder_id"),
        Index("ix_outreach_status", "status"),
        Index("ix_outreach_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    founder_id: Mapped[int] = mapped_column(ForeignKey("founders.id", ondelete="CASCADE"))
    status: Mapped[OutreachStatus] = mapped_column(_enum(OutreachStatus))
    exclusion_reason: Mapped[ExclusionReason | None] = mapped_column(_enum(ExclusionReason))
    exclusion_detail: Mapped[str | None] = mapped_column(Text)
    close_reason: Mapped[CloseReason | None] = mapped_column(_enum(CloseReason))
    email: Mapped[str | None] = mapped_column(String(320))
    email_source: Mapped[EmailSource | None] = mapped_column(_enum(EmailSource))
    draft_subject: Mapped[str | None] = mapped_column(Text)
    draft_body: Mapped[str | None] = mapped_column(Text)
    template_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    drafted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deck_received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_tracked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class StatusEventRow(Base):
    __tablename__ = "status_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outreach_id: Mapped[int] = mapped_column(ForeignKey("outreach.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[OutreachStatus] = mapped_column(_enum(OutreachStatus))
    to_status: Mapped[OutreachStatus] = mapped_column(_enum(OutreachStatus))
    source: Mapped[EventSource] = mapped_column(_enum(EventSource))
    at: Mapped[datetime] = mapped_column(UTCDateTime())
    note: Mapped[str | None] = mapped_column(Text)


class EmailLookupRow(Base):
    __tablename__ = "email_lookups"
    __table_args__ = (UniqueConstraint("founder_id", "provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    founder_id: Mapped[int] = mapped_column(ForeignKey("founders.id", ondelete="CASCADE"))
    provider: Mapped[EmailSource] = mapped_column(_enum(EmailSource))
    outcome: Mapped[LookupOutcome] = mapped_column(_enum(LookupOutcome))
    email: Mapped[str | None] = mapped_column(String(320))
    grade: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # Every verified address, best first: [{"email", "grade", "smtp_valid", "reason"}].
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[RunTrigger] = mapped_column(_enum(RunTrigger))
    state: Mapped[RunState] = mapped_column(_enum(RunState))
    dry_run: Mapped[bool] = mapped_column(Boolean)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    counts: Mapped[dict[str, int]] = mapped_column(JSON)
    errors: Mapped[list[str]] = mapped_column(JSON)


class LockRow(Base):
    """A named lock. ``owner`` is a random per-holder token, so only the holder can
    refresh (heartbeat) or release it; NULL only for the legacy ownerless API."""

    __tablename__ = "locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime())
    owner: Mapped[str | None] = mapped_column(String(64))


class LookupBudgetRow(Base):
    """Paid lookups *attempted* per provider per UTC day (the daily cap).

    Incremented with a conditional UPDATE before every paid call, so the cap holds
    even if recording the result later fails, and across concurrent runs. No PII.
    """

    __tablename__ = "lookup_budget"

    provider: Mapped[EmailSource] = mapped_column(_enum(EmailSource), primary_key=True)
    day_start: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    used: Mapped[int] = mapped_column(Integer)


class SuppressionRow(Base):
    """An erased founder: only a keyed hash (HMAC) of an identifier, never the
    identifier itself, so re-ingestion can be refused without keeping PII."""

    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("key_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class AdminSessionRow(Base):
    """A live dashboard session. Only a SHA-256 of the random session id is stored,
    so a database dump cannot be replayed as a cookie."""

    __tablename__ = "admin_sessions"

    sid_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class TemplateRow(Base):
    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
