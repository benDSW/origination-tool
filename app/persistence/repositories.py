"""``SqlStore``: the SQLAlchemy implementation of :class:`app.domain.ports.Store`.

Every public method is one transaction (``with session.begin()``) and returns
domain DTOs only; ORM rows never leave this module. Integrity errors are turned
into domain-level exceptions *without* chaining the DB error, because driver
exceptions embed the bound parameters (founder names, emails) in their text.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.models import (
    EmailCandidate,
    EmailLookupRecord,
    EmailSource,
    EmailTemplate,
    FounderRecord,
    LookupOutcome,
    OutreachRecord,
    OutreachStatus,
    OutreachView,
    RunCounts,
    RunRecord,
    SavedSearch,
    SearchConfig,
    StatusEvent,
)
from app.domain.ports import LookupReservation, NotFoundError
from app.domain.templating import DEFAULT_TEMPLATE
from app.persistence.models import (
    AdminSessionRow,
    EmailLookupRow,
    FounderRow,
    FounderSearchRow,
    LockRow,
    LookupBudgetRow,
    OutreachRow,
    RunRow,
    SearchRow,
    StatusEventRow,
    SuppressionRow,
    TemplateRow,
)

_RowT = TypeVar("_RowT")
_T = TypeVar("_T")

_SEARCH_FIELDS = (
    "external_id",
    "title",
    "enabled",
    "location_policy",
    "lookback_days",
    "watermark",
    "last_synced_at",
)
_FOUNDER_FIELDS = tuple(f for f in FounderRecord.model_fields if f != "id")
_OUTREACH_FIELDS = tuple(f for f in OutreachRecord.model_fields if f != "id")


RESERVED = LookupReservation.RESERVED
CAP_REACHED = LookupReservation.CAP_REACHED
ALREADY_CLAIMED = LookupReservation.ALREADY_CLAIMED


class _Outcome(Exception):
    """Internal: leave a transaction early (rolling it back) with a result."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.value = value


class ConflictError(ValueError):
    """A uniqueness rule was violated (e.g. a second outreach for one founder).

    Subclasses ValueError so callers that only know the domain can still catch it.
    """


# --------------------------------------------------------------------------- #
# Row <-> DTO mapping
# --------------------------------------------------------------------------- #


def _copy(row: Any, data: dict[str, Any], fields: tuple[str, ...]) -> None:
    for name in fields:
        setattr(row, name, data[name])


def _search_dto(row: SearchRow) -> SearchConfig:
    return SearchConfig(id=row.id, **{f: getattr(row, f) for f in _SEARCH_FIELDS})


def _founder_dto(row: FounderRow) -> FounderRecord:
    return FounderRecord(id=row.id, **{f: getattr(row, f) for f in _FOUNDER_FIELDS})


def _outreach_dto(row: OutreachRow) -> OutreachRecord:
    return OutreachRecord(id=row.id, **{f: getattr(row, f) for f in _OUTREACH_FIELDS})


def _event_dto(row: StatusEventRow) -> StatusEvent:
    return StatusEvent(
        outreach_id=row.outreach_id,
        from_status=row.from_status,
        to_status=row.to_status,
        source=row.source,
        at=row.at,
        note=row.note,
    )


def _event_row(event: StatusEvent) -> StatusEventRow:
    return StatusEventRow(
        outreach_id=event.outreach_id,
        from_status=event.from_status,
        to_status=event.to_status,
        source=event.source,
        at=event.at,
        note=event.note,
    )


def _lookup_dto(row: EmailLookupRow) -> EmailLookupRecord:
    return EmailLookupRecord(
        founder_id=row.founder_id,
        provider=row.provider,
        outcome=row.outcome,
        email=row.email,
        grade=row.grade,
        reason=row.reason,
        candidates=_candidates_from_json(row.candidates),
        created_at=row.created_at,
    )


def _candidates_to_json(candidates: Sequence[EmailCandidate]) -> list[dict[str, Any]] | None:
    return [c.model_dump() for c in candidates] or None


def _candidates_from_json(raw: object) -> tuple[EmailCandidate, ...]:
    # Skip anything malformed rather than failing the whole page.
    if not isinstance(raw, list):
        return ()
    parsed: list[EmailCandidate] = []
    for item in raw:
        try:
            parsed.append(EmailCandidate.model_validate(item))
        except ValueError:
            continue
    return tuple(parsed)


def _run_counts(raw: dict[str, Any] | None) -> RunCounts:
    # Ignore keys a newer/older version wrote, so old rows always load.
    known = {k: v for k, v in (raw or {}).items() if k in RunCounts.model_fields}
    return RunCounts.model_validate(known)


def _run_dto(row: RunRow) -> RunRecord:
    return RunRecord(
        id=row.id,
        trigger=row.trigger,
        state=row.state,
        dry_run=row.dry_run,
        started_at=row.started_at,
        finished_at=row.finished_at,
        counts=_run_counts(row.counts),
        errors=[str(e) for e in (row.errors or [])],
    )


def _apply_run(row: RunRow, run: RunRecord) -> None:
    row.trigger = run.trigger
    row.state = run.state
    row.dry_run = run.dry_run
    row.started_at = run.started_at
    row.finished_at = run.finished_at
    row.counts = run.counts.model_dump(mode="json")
    row.errors = list(run.errors)


def _template_dto(row: TemplateRow) -> EmailTemplate:
    return EmailTemplate(subject=row.subject, body=row.body, version=row.version)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class SqlStore:
    """Implements :class:`app.domain.ports.Store` on SQLite or Postgres."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _tx(self) -> Iterator[Session]:
        try:
            with self._session_factory() as session, session.begin():
                yield session
        except StatementError as exc:
            # A bind-time ValueError (e.g. a naive datetime) is a caller bug: surface it
            # as a plain ValueError without the SQL statement and parameters attached.
            if isinstance(exc.orig, ValueError) and not isinstance(exc, DBAPIError):
                raise ValueError(str(exc.orig)) from None
            raise

    def _retry_on_conflict(self, op: Callable[[], _T]) -> _T:
        """Run ``op`` once more if a concurrent writer won an insert race."""
        try:
            return op()
        except IntegrityError:
            return op()

    @staticmethod
    def _get(session: Session, model: type[_RowT], row_id: int, what: str) -> _RowT:
        row = session.get(model, row_id)
        if row is None:
            raise NotFoundError(f"{what} {row_id} not found")
        return row

    # ------------------------------------------------------------------ searches

    def list_search_configs(self) -> list[SearchConfig]:
        with self._tx() as s:
            rows = s.scalars(select(SearchRow).order_by(SearchRow.title, SearchRow.id))
            return [_search_dto(r) for r in rows]

    def get_search_config(self, search_id: int) -> SearchConfig:
        with self._tx() as s:
            return _search_dto(self._get(s, SearchRow, search_id, "search"))

    def upsert_search_from_catalog(self, saved: SavedSearch, *, synced_at: datetime) -> SearchConfig:
        def op() -> SearchConfig:
            with self._tx() as s:
                row = s.scalars(select(SearchRow).where(SearchRow.external_id == saved.external_id)).first()
                if row is None:
                    defaults = SearchConfig(external_id=saved.external_id, title=saved.title)
                    row = SearchRow(**{f: getattr(defaults, f) for f in _SEARCH_FIELDS})
                    s.add(row)
                # Only catalogue-owned fields; local settings are never touched.
                row.title = saved.title
                row.last_synced_at = synced_at
                s.flush()
                return _search_dto(row)

        return self._retry_on_conflict(op)

    def save_search_config(self, config: SearchConfig) -> SearchConfig:
        data = config.model_dump()
        try:
            with self._tx() as s:
                if config.id is None:
                    row = SearchRow()
                    s.add(row)
                else:
                    row = self._get(s, SearchRow, config.id, "search")
                _copy(row, data, _SEARCH_FIELDS)
                s.flush()
                return _search_dto(row)
        except IntegrityError:
            raise ConflictError("a search with this external id already exists") from None

    # ------------------------------------------------------------------ founders

    def find_founder(self, *, evertrace_signal_id: str, linkedin_slug: str | None) -> FounderRecord | None:
        condition = FounderRow.evertrace_signal_id == evertrace_signal_id
        if linkedin_slug is not None:
            condition = condition | (FounderRow.linkedin_slug == linkedin_slug)
        with self._tx() as s:
            rows = list(s.scalars(select(FounderRow).where(condition).order_by(FounderRow.id).limit(2)))
            if not rows:
                return None
            # Two different rows can match (one by id, one by slug): the exact
            # signal-id match is the stronger identity.
            exact = [r for r in rows if r.evertrace_signal_id == evertrace_signal_id]
            return _founder_dto((exact or rows)[0])

    def add_founder(self, founder: FounderRecord) -> FounderRecord:
        try:
            with self._tx() as s:
                row = FounderRow()
                _copy(row, founder.model_dump(), _FOUNDER_FIELDS)
                s.add(row)
                s.flush()
                return _founder_dto(row)
        except IntegrityError:
            raise ConflictError("a founder with this signal id or LinkedIn slug already exists") from None

    def get_founder(self, founder_id: int) -> FounderRecord:
        with self._tx() as s:
            return _founder_dto(self._get(s, FounderRow, founder_id, "founder"))

    def link_founder_to_search(self, founder_id: int, search_id: int) -> None:
        def op() -> None:
            with self._tx() as s:
                self._get(s, FounderRow, founder_id, "founder")
                self._get(s, SearchRow, search_id, "search")
                exists = s.scalar(
                    select(FounderSearchRow.id).where(
                        FounderSearchRow.founder_id == founder_id, FounderSearchRow.search_id == search_id
                    )
                )
                if exists is None:
                    s.add(FounderSearchRow(founder_id=founder_id, search_id=search_id))

        self._retry_on_conflict(op)

    def delete_founder(self, founder_id: int) -> None:
        with self._tx() as s:
            self._delete_founder(s, founder_id)

    def _delete_founder(self, s: Session, founder_id: int) -> None:
        self._get(s, FounderRow, founder_id, "founder")
        # Explicit deletes as well as ON DELETE CASCADE: erasure must be
        # complete even on a connection where SQLite FK enforcement is off.
        outreach_ids = select(OutreachRow.id).where(OutreachRow.founder_id == founder_id)
        s.execute(delete(StatusEventRow).where(StatusEventRow.outreach_id.in_(outreach_ids)))
        s.execute(delete(OutreachRow).where(OutreachRow.founder_id == founder_id))
        s.execute(delete(EmailLookupRow).where(EmailLookupRow.founder_id == founder_id))
        s.execute(delete(FounderSearchRow).where(FounderSearchRow.founder_id == founder_id))
        s.execute(delete(FounderRow).where(FounderRow.id == founder_id))

    # -------------------------------------------------------------- suppressions

    def erase_founder(self, founder_id: int, *, suppress: Sequence[str], at: datetime) -> None:
        """Right to erasure that sticks: delete everything about the founder and, in the
        same transaction, record the given keyed hashes so they are never re-ingested.
        The hashes are opaque (HMAC) and are the only thing kept."""
        wanted = sorted({h for h in suppress if h})

        def op() -> None:
            with self._tx() as s:
                self._delete_founder(s, founder_id)
                if wanted:
                    existing = set(
                        s.scalars(select(SuppressionRow.key_hash).where(SuppressionRow.key_hash.in_(wanted)))
                    )
                    for key_hash in wanted:
                        if key_hash not in existing:
                            s.add(SuppressionRow(key_hash=key_hash, created_at=at))

        self._retry_on_conflict(op)

    def is_suppressed(self, key_hashes: Sequence[str]) -> bool:
        wanted = sorted({h for h in key_hashes if h})
        if not wanted:
            return False
        with self._tx() as s:
            hit = s.scalar(select(SuppressionRow.id).where(SuppressionRow.key_hash.in_(wanted)).limit(1))
            return hit is not None

    # ------------------------------------------------------------------ outreach

    def add_outreach(self, outreach: OutreachRecord) -> OutreachRecord:
        try:
            with self._tx() as s:
                self._get(s, FounderRow, outreach.founder_id, "founder")
                row = OutreachRow()
                _copy(row, outreach.model_dump(), _OUTREACH_FIELDS)
                s.add(row)
                s.flush()
                return _outreach_dto(row)
        except IntegrityError:
            raise ConflictError("this founder already has an outreach record") from None

    def get_outreach(self, outreach_id: int) -> OutreachRecord:
        with self._tx() as s:
            return _outreach_dto(self._get(s, OutreachRow, outreach_id, "outreach"))

    def get_outreach_for_founder(self, founder_id: int) -> OutreachRecord | None:
        with self._tx() as s:
            row = s.scalars(select(OutreachRow).where(OutreachRow.founder_id == founder_id)).first()
            return _outreach_dto(row) if row is not None else None

    def save_outreach(self, outreach: OutreachRecord, event: StatusEvent | None = None) -> OutreachRecord:
        if outreach.id is None:
            raise ValueError("save_outreach needs a persisted record; use add_outreach first")
        if event is not None and event.outreach_id != outreach.id:
            raise ValueError("status event belongs to a different outreach record")
        try:
            with self._tx() as s:
                row = self._get(s, OutreachRow, outreach.id, "outreach")
                _copy(row, outreach.model_dump(), _OUTREACH_FIELDS)
                if event is not None:
                    s.add(_event_row(event))
                s.flush()
                return _outreach_dto(row)
        except IntegrityError:
            raise ConflictError("this founder already has an outreach record") from None

    def _views(self, s: Session, stmt: Select[tuple[OutreachRow, FounderRow]]) -> list[OutreachView]:
        pairs = [(o, f) for o, f in s.execute(stmt)]
        titles: dict[int, list[str]] = {}
        founder_ids = [f.id for _, f in pairs]
        if founder_ids:
            title_rows = s.execute(
                select(FounderSearchRow.founder_id, SearchRow.title)
                .join(SearchRow, SearchRow.id == FounderSearchRow.search_id)
                .where(FounderSearchRow.founder_id.in_(founder_ids))
                .order_by(SearchRow.title)
            )
            for fid, title in title_rows:
                titles.setdefault(fid, []).append(title)
        return [
            OutreachView(
                outreach=_outreach_dto(o), founder=_founder_dto(f), search_titles=tuple(titles.get(f.id, ()))
            )
            for o, f in pairs
        ]

    @staticmethod
    def _view_select() -> Select[tuple[OutreachRow, FounderRow]]:
        return select(OutreachRow, FounderRow).join(FounderRow, FounderRow.id == OutreachRow.founder_id)

    def list_outreach(
        self, statuses: frozenset[OutreachStatus] | None = None, *, limit: int = 500
    ) -> list[OutreachView]:
        stmt = self._view_select()
        if statuses is not None:
            stmt = stmt.where(OutreachRow.status.in_(sorted(statuses)))
        stmt = stmt.order_by(OutreachRow.updated_at.desc(), OutreachRow.id.desc()).limit(max(0, limit))
        with self._tx() as s:
            return self._views(s, stmt)

    def get_outreach_view(self, outreach_id: int) -> OutreachView:
        with self._tx() as s:
            views = self._views(s, self._view_select().where(OutreachRow.id == outreach_id))
            if not views:
                raise NotFoundError(f"outreach {outreach_id} not found")
            return views[0]

    def count_by_status(self) -> dict[OutreachStatus, int]:
        """Every status is present (zero when empty) so the dashboard needs no defaults."""
        counts = dict.fromkeys(OutreachStatus, 0)
        with self._tx() as s:
            for status, n in s.execute(select(OutreachRow.status, func.count()).group_by(OutreachRow.status)):
                counts[status] = n
        return counts

    def list_status_events(self, outreach_id: int) -> list[StatusEvent]:
        with self._tx() as s:
            rows = s.scalars(
                select(StatusEventRow)
                .where(StatusEventRow.outreach_id == outreach_id)
                .order_by(StatusEventRow.at, StatusEventRow.id)
            )
            return [_event_dto(r) for r in rows]

    # ------------------------------------------------------------ email lookups

    def get_email_lookup(self, founder_id: int, provider: EmailSource) -> EmailLookupRecord | None:
        with self._tx() as s:
            row = s.scalars(
                select(EmailLookupRow).where(
                    EmailLookupRow.founder_id == founder_id, EmailLookupRow.provider == provider
                )
            ).first()
            return _lookup_dto(row) if row is not None else None

    def list_email_lookups(
        self, founder_ids: Sequence[int], provider: EmailSource
    ) -> dict[int, EmailLookupRecord]:
        if not founder_ids:
            return {}
        with self._tx() as s:
            rows = s.scalars(
                select(EmailLookupRow).where(
                    EmailLookupRow.provider == provider, EmailLookupRow.founder_id.in_(set(founder_ids))
                )
            )
            return {row.founder_id: _lookup_dto(row) for row in rows}

    def add_email_lookup(self, record: EmailLookupRecord) -> None:
        def op() -> None:
            with self._tx() as s:
                self._get(s, FounderRow, record.founder_id, "founder")
                row = s.scalars(
                    select(EmailLookupRow).where(
                        EmailLookupRow.founder_id == record.founder_id,
                        EmailLookupRow.provider == record.provider,
                    )
                ).first()
                if row is None:
                    row = EmailLookupRow(founder_id=record.founder_id, provider=record.provider)
                    s.add(row)
                row.outcome = record.outcome
                row.email = record.email
                row.grade = record.grade
                row.reason = record.reason
                row.candidates = _candidates_to_json(record.candidates)
                row.created_at = record.created_at

        self._retry_on_conflict(op)

    def reserve_email_lookup(
        self, reservation: EmailLookupRecord, *, day_start: datetime, daily_cap: int
    ) -> str:
        """Claim one paid lookup *before* the provider is called. One transaction:

        1. claim the founder's single cache row (insert it, or take over an ERROR row
           with a conditional UPDATE), so two runs can never look the same person up;
        2. take one unit of today's budget with ``UPDATE ... SET used = used + 1
           WHERE used < cap``, which is atomic on SQLite and Postgres alike.

        Returns ``RESERVED``, ``CAP_REACHED`` (nothing written) or ``ALREADY_CLAIMED``
        (another run or an earlier attempt owns this founder's lookup).
        """
        founder_id, provider = reservation.founder_id, reservation.provider
        values = {
            "outcome": reservation.outcome,
            "email": reservation.email,
            "grade": reservation.grade,
            "reason": reservation.reason,
            "candidates": _candidates_to_json(reservation.candidates),
            "created_at": reservation.created_at,
        }

        def op() -> str:
            try:
                with self._tx() as s:
                    self._get(s, FounderRow, founder_id, "founder")
                    row = s.scalars(
                        select(EmailLookupRow).where(
                            EmailLookupRow.founder_id == founder_id, EmailLookupRow.provider == provider
                        )
                    ).first()
                    if row is None:
                        s.add(EmailLookupRow(founder_id=founder_id, provider=provider, **values))
                        s.flush()  # a concurrent claim fails here on the unique constraint
                    elif row.outcome is LookupOutcome.ERROR:
                        claimed = s.execute(
                            update(EmailLookupRow)
                            .where(EmailLookupRow.id == row.id, EmailLookupRow.outcome == LookupOutcome.ERROR)
                            .values(**values)
                            .execution_options(synchronize_session=False)
                        )
                        if claimed.rowcount != 1:  # type: ignore[attr-defined]  # pragma: no cover - race
                            raise _Outcome(ALREADY_CLAIMED)
                    else:
                        raise _Outcome(ALREADY_CLAIMED)
                    if not self._take_budget(s, provider, day_start, daily_cap, founder_id):
                        raise _Outcome(CAP_REACHED)  # rolls the claim back too
                return RESERVED
            except _Outcome as outcome:
                return outcome.value

        return self._retry_on_conflict(op)

    @staticmethod
    def _take_budget(
        s: Session, provider: EmailSource, day_start: datetime, cap: int, founder_id: int
    ) -> bool:
        if s.get(LookupBudgetRow, (provider, day_start)) is None:
            # First reservation today: start from lookups already cached today (e.g.
            # recorded before this table existed), excluding the row just claimed.
            seed = s.scalar(
                select(func.count())
                .select_from(EmailLookupRow)
                .where(
                    EmailLookupRow.provider == provider,
                    EmailLookupRow.created_at >= day_start,
                    EmailLookupRow.founder_id != founder_id,
                )
            )
            s.add(LookupBudgetRow(provider=provider, day_start=day_start, used=int(seed or 0)))
            s.flush()  # a concurrent first reservation fails here and is retried
        taken = s.execute(
            update(LookupBudgetRow)
            .where(
                LookupBudgetRow.provider == provider,
                LookupBudgetRow.day_start == day_start,
                LookupBudgetRow.used < cap,
            )
            .values(used=LookupBudgetRow.used + 1)
            .execution_options(synchronize_session=False)
        )
        return bool(taken.rowcount == 1)  # type: ignore[attr-defined]

    def lookups_reserved_since(self, provider: EmailSource, day_start: datetime) -> int:
        """Paid lookups attempted since ``day_start`` (0 before the first reservation)."""
        with self._tx() as s:
            row = s.get(LookupBudgetRow, (provider, day_start))
            return row.used if row is not None else 0

    def count_email_lookups_since(self, provider: EmailSource, since: datetime) -> int:
        with self._tx() as s:
            n = s.scalar(
                select(func.count())
                .select_from(EmailLookupRow)
                .where(EmailLookupRow.provider == provider, EmailLookupRow.created_at >= since)
            )
            return int(n or 0)

    # ------------------------------------------------------------- runs + locks

    def acquire_owned_lock(self, name: str, *, owner: str, now: datetime, ttl_seconds: int) -> bool:
        """Take the lock, recording ``owner`` so that only this holder can heartbeat
        (:meth:`refresh_owned_lock`) or release it.

        Fast path: the primary key makes the insert atomic. If held, take it over only
        when stale - the WHERE makes check-and-set one statement, so two contenders
        cannot both succeed."""
        try:
            with self._tx() as s:
                s.add(LockRow(name=name, acquired_at=now, owner=owner))
            return True
        except IntegrityError:
            pass
        cutoff = now - timedelta(seconds=ttl_seconds)
        with self._tx() as s:
            result = s.execute(
                update(LockRow)
                .where(LockRow.name == name, LockRow.acquired_at < cutoff)
                .values(acquired_at=now, owner=owner)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1  # type: ignore[attr-defined]

    def refresh_owned_lock(self, name: str, *, owner: str, now: datetime) -> bool:
        """Heartbeat. False means the lock is gone or was taken over: stop working."""
        with self._tx() as s:
            result = s.execute(
                update(LockRow)
                .where(LockRow.name == name, LockRow.owner == owner)
                .values(acquired_at=now)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1  # type: ignore[attr-defined]

    def release_owned_lock(self, name: str, *, owner: str) -> None:
        """Release only if still held by ``owner``: a run that lost its lock never
        deletes the lock of the run that took over."""
        with self._tx() as s:
            s.execute(delete(LockRow).where(LockRow.name == name, LockRow.owner == owner))

    # ------------------------------------------------------------ admin sessions

    def add_admin_session(self, sid_hash: str, *, created_at: datetime, expires_at: datetime) -> None:
        with self._tx() as s:
            s.execute(delete(AdminSessionRow).where(AdminSessionRow.expires_at <= created_at))
            s.add(AdminSessionRow(sid_hash=sid_hash, created_at=created_at, expires_at=expires_at))

    def admin_session_active(self, sid_hash: str, *, now: datetime) -> bool:
        with self._tx() as s:
            hit = s.scalar(
                select(AdminSessionRow.sid_hash).where(
                    AdminSessionRow.sid_hash == sid_hash, AdminSessionRow.expires_at > now
                )
            )
            return hit is not None

    def delete_admin_session(self, sid_hash: str) -> None:
        with self._tx() as s:
            s.execute(delete(AdminSessionRow).where(AdminSessionRow.sid_hash == sid_hash))

    def delete_all_admin_sessions(self) -> int:
        with self._tx() as s:
            result = s.execute(delete(AdminSessionRow))
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def add_run(self, run: RunRecord) -> RunRecord:
        with self._tx() as s:
            row = RunRow()
            _apply_run(row, run)
            s.add(row)
            s.flush()
            return _run_dto(row)

    def save_run(self, run: RunRecord) -> RunRecord:
        if run.id is None:
            raise ValueError("save_run needs a persisted run; use add_run first")
        with self._tx() as s:
            row = self._get(s, RunRow, run.id, "run")
            _apply_run(row, run)
            s.flush()
            return _run_dto(row)

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._tx() as s:
            rows = s.scalars(
                select(RunRow).order_by(RunRow.started_at.desc(), RunRow.id.desc()).limit(max(0, limit))
            )
            return [_run_dto(r) for r in rows]

    def latest_run(self) -> RunRecord | None:
        runs = self.list_runs(limit=1)
        return runs[0] if runs else None

    # ----------------------------------------------------------------- template

    @staticmethod
    def _latest_template(s: Session) -> TemplateRow | None:
        return s.scalars(select(TemplateRow).order_by(TemplateRow.version.desc()).limit(1)).first()

    @staticmethod
    def _seed_template(s: Session) -> TemplateRow:
        row = TemplateRow(
            version=DEFAULT_TEMPLATE.version,
            subject=DEFAULT_TEMPLATE.subject,
            body=DEFAULT_TEMPLATE.body,
            created_at=datetime.now(UTC),
        )
        s.add(row)
        s.flush()
        return row

    def get_template(self) -> EmailTemplate:
        def op() -> EmailTemplate:
            with self._tx() as s:
                row = self._latest_template(s) or self._seed_template(s)
                return _template_dto(row)

        return self._retry_on_conflict(op)

    def save_template(self, subject: str, body: str) -> EmailTemplate:
        def op() -> EmailTemplate:
            with self._tx() as s:
                # Seed first so the default stays in the history as version 1.
                previous = self._latest_template(s) or self._seed_template(s)
                row = TemplateRow(
                    version=previous.version + 1, subject=subject, body=body, created_at=datetime.now(UTC)
                )
                s.add(row)
                s.flush()
                return _template_dto(row)

        return self._retry_on_conflict(op)

    def list_template_versions(self) -> list[EmailTemplate]:
        """History, newest first (not part of the Store port; handy for audits)."""
        with self._tx() as s:
            rows = s.scalars(select(TemplateRow).order_by(TemplateRow.version.desc()))
            return [_template_dto(r) for r in rows]
