"""Dashboard actions on outreach (R7, R11, R16, R17).

Every status change goes through :func:`app.domain.status.transition`, so manual
clicks follow the same state machine as the pipeline and are audited with
``source=MANUAL``. Errors are raised as :class:`ServiceError` subclasses whose
messages are safe to show in the dashboard.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.models import (
    CloseReason,
    Draft,
    EmailCandidate,
    EmailLookupRecord,
    EmailSource,
    EventSource,
    ExclusionReason,
    FounderRecord,
    LookupOutcome,
    OutreachRecord,
    OutreachStatus,
    OutreachView,
    RunRecord,
    StatusEvent,
)
from app.domain.ports import EmailFinder, NotFoundError, Store
from app.domain.status import InvalidTransition, transition
from app.domain.templating import TemplateError, build_draft, normalise_email
from app.services.clock import Clock, utc_now, whole_days_between
from app.services.drafting import current_draft, draft_outreach, write_draft
from app.services.errors import InvalidAction, NotFound, ValidationFailed
from app.services.suppression import SuppressionKeys

logger = logging.getLogger(__name__)

S = OutreachStatus
# Replied first: that is the column that needs a decision (deck received / declined).
PIPELINE_STATUSES: tuple[OutreachStatus, ...] = (S.REPLIED, S.SENT, S.DECK_RECEIVED)
# Statuses in which the user can still pick a different RocketReach address.
CHOOSABLE_STATUSES: frozenset[OutreachStatus] = frozenset({S.NEEDS_EMAIL, S.DRAFTED})
CREDITS_CACHE_SECONDS = 300
MAX_LIST = 2000


# --------------------------------------------------------------------------- #
# Read models for the dashboard
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OutreachItem:
    """One row in a dashboard list: the outreach + founder, with waiting time.

    ``days_waiting`` is days since the step the founder is waiting on: since the
    draft (DRAFTED), since sending (SENT, i.e. awaiting a reply), since the reply
    (REPLIED, awaiting a deck). None for other statuses.

    ``email_options`` is every verified address RocketReach found, best first
    (empty when there was no lookup, or it predates keeping alternatives).
    """

    view: OutreachView
    days_waiting: int | None
    follow_up_due: bool
    email_options: tuple[EmailCandidate, ...] = ()

    @property
    def can_choose_email(self) -> bool:
        return self.view.outreach.status in CHOOSABLE_STATUSES and bool(self.email_options)

    @property
    def outreach(self) -> OutreachRecord:
        return self.view.outreach

    @property
    def founder(self) -> FounderRecord:
        return self.view.founder

    @property
    def draft(self) -> Draft | None:
        if self.view.outreach.status is not S.DRAFTED:
            return None
        return current_draft(self.view.outreach)


@dataclass(frozen=True)
class OutreachDetail:
    item: OutreachItem
    events: list[StatusEvent]  # oldest first
    rocketreach_lookup: EmailLookupRecord | None

    @property
    def outreach(self) -> OutreachRecord:
        return self.item.outreach

    @property
    def founder(self) -> FounderRecord:
        return self.item.founder

    @property
    def draft(self) -> Draft | None:
        return self.item.draft


@dataclass(frozen=True)
class DashboardSummary:
    counts: dict[OutreachStatus, int]
    latest_run: RunRecord | None
    rocketreach_credits: int | None  # None: no finder configured, or unknown
    follow_ups_due: list[OutreachItem]
    follow_up_after_days: int
    generated_at: datetime


@dataclass
class _CreditsCache:
    value: int | None = None
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class PipelineBoard:
    """Replied / Sent / Deck received columns (R17)."""

    columns: dict[OutreachStatus, list[OutreachItem]] = field(default_factory=dict)
    # Most recent automatic reply check of anything still awaiting a reply.
    last_checked_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Outreach actions
# --------------------------------------------------------------------------- #


class OutreachService:
    def __init__(
        self,
        store: Store,
        email_finder: EmailFinder | None,
        clock: Clock = utc_now,
        *,
        follow_up_after_days: int = 14,
        suppression: SuppressionKeys | None = None,
    ) -> None:
        self._store = store
        self._suppression = suppression or SuppressionKeys.development()
        self._finder = email_finder
        self._clock = clock
        self._follow_up = timedelta(days=follow_up_after_days)
        self._follow_up_days = follow_up_after_days
        self._credits = _CreditsCache()

    # ------------------------------------------------------------------ reads

    def list_by_status(
        self, statuses: Iterable[OutreachStatus] | None = None, *, limit: int = 500
    ) -> list[OutreachItem]:
        """Newest activity first. ``None`` lists everything."""
        wanted = frozenset(statuses) if statuses is not None else None
        now = self._clock()
        views = self._store.list_outreach(wanted, limit=max(1, min(limit, MAX_LIST)))
        lookups = self._store.list_email_lookups(
            [v.founder.id for v in views if v.founder.id is not None], EmailSource.ROCKETREACH
        )
        return [self._item(v, now, lookups.get(v.founder.id or 0)) for v in views]

    def detail(self, outreach_id: int) -> OutreachDetail:
        view = self._view(outreach_id)
        founder_id = view.founder.id
        lookup = (
            self._store.get_email_lookup(founder_id, EmailSource.ROCKETREACH)
            if founder_id is not None
            else None
        )
        return OutreachDetail(
            item=self._item(view, self._clock(), lookup),
            events=self._store.list_status_events(outreach_id),
            rocketreach_lookup=lookup,
        )

    def pipeline(self) -> PipelineBoard:
        """Replied / Sent / Deck received, longest-waiting first."""
        items = self.list_by_status(PIPELINE_STATUSES, limit=MAX_LIST)
        columns: dict[OutreachStatus, list[OutreachItem]] = {s: [] for s in PIPELINE_STATUSES}
        for item in items:
            columns[item.outreach.status].append(item)
        for column in columns.values():
            column.sort(key=lambda i: -(i.days_waiting or 0))
        checks = [i.outreach.last_tracked_at for i in columns[S.SENT] if i.outreach.last_tracked_at]
        return PipelineBoard(columns=columns, last_checked_at=max(checks, default=None))

    def follow_ups_due(self) -> list[OutreachItem]:
        return [i for i in self.list_by_status([S.SENT], limit=MAX_LIST) if i.follow_up_due]

    def dashboard_summary(self) -> DashboardSummary:
        now = self._clock()
        follow_ups = sorted(self.follow_ups_due(), key=lambda i: -(i.days_waiting or 0))
        return DashboardSummary(
            counts=self._store.count_by_status(),
            latest_run=self._store.latest_run(),
            rocketreach_credits=self._remaining_credits(now),
            follow_ups_due=follow_ups,
            follow_up_after_days=self._follow_up_days,
            generated_at=now,
        )

    # --------------------------------------------------------- status buttons

    def mark_sent(self, outreach_id: int) -> OutreachRecord:
        return self._move(outreach_id, S.SENT)

    def mark_replied(self, outreach_id: int) -> OutreachRecord:
        return self._move(outreach_id, S.REPLIED)

    def mark_deck_received(self, outreach_id: int) -> OutreachRecord:
        return self._move(outreach_id, S.DECK_RECEIVED)

    def close(self, outreach_id: int, reason: CloseReason | str = CloseReason.SKIPPED) -> OutreachRecord:
        try:
            close_reason = CloseReason(reason)
        except ValueError:
            raise ValidationFailed(["Unknown close reason"]) from None
        return self._move(outreach_id, S.CLOSED, close_reason=close_reason)

    def discard_email(self, outreach_id: int) -> OutreachRecord:
        """DRAFTED -> NEEDS_EMAIL: the address was wrong; the draft is dropped."""
        return self._move(outreach_id, S.NEEDS_EMAIL, note="Email discarded")

    # ------------------------------------------------------------ email entry

    def set_manual_email(self, outreach_id: int, email: str) -> OutreachRecord:
        """NEEDS_EMAIL / EXCLUDED -> DRAFTED with a typed-in address (R11)."""
        address = normalise_email(email)
        if address is None:
            raise ValidationFailed(["That email address is not valid"])
        view = self._view(outreach_id)
        outreach = view.outreach
        if outreach.status not in (S.NEEDS_EMAIL, S.EXCLUDED):
            raise InvalidAction(f"An email can only be added while the founder is {_label(S.NEEDS_EMAIL)}")
        return self._draft(view, address, EmailSource.MANUAL, note="Email entered manually")

    def choose_email(self, outreach_id: int, email: str) -> OutreachRecord:
        """Use another verified RocketReach address: re-renders the draft (DRAFTED) or
        creates it (NEEDS_EMAIL). Only addresses RocketReach returned are accepted."""
        address = normalise_email(email)
        view = self._view(outreach_id)
        outreach, founder = view.outreach, view.founder
        options = {c.email.casefold(): c.email for c in self._email_options(founder)}
        if address is None or address.casefold() not in options:
            raise ValidationFailed(["That address is not one RocketReach found for this founder"])
        address = options[address.casefold()]  # RocketReach's spelling, not the submitted one
        if outreach.status is S.NEEDS_EMAIL:
            return self._draft(view, address, EmailSource.ROCKETREACH, note="RocketReach address chosen")
        if outreach.status is not S.DRAFTED:
            raise InvalidAction("The address can only be changed before the email is sent")
        if outreach.email is not None and outreach.email.casefold() == address.casefold():
            return outreach
        # Same status, so no audit event (as for re-rendering after a template edit).
        template = self._store.get_template()
        try:
            draft = build_draft(template, founder, address)
        except TemplateError:
            raise ValidationFailed(["That email address is not valid"]) from None
        write_draft(outreach, draft, template)
        outreach.email_source = EmailSource.ROCKETREACH
        outreach.updated_at = self._clock()
        try:
            return self._store.save_outreach(outreach)
        except NotFoundError:
            raise NotFound("That outreach record no longer exists") from None

    def draft_anyway(self, outreach_id: int) -> OutreachRecord:
        """Override an exclusion (R7): draft if an email is known, else NEEDS_EMAIL."""
        view = self._view(outreach_id)
        outreach, founder = view.outreach, view.founder
        if outreach.status is not S.EXCLUDED:
            raise InvalidAction("Only excluded founders can be drafted anyway")
        note = f"Exclusion overridden ({_reason_label(outreach.exclusion_reason)})"
        known = self._known_email(founder)
        if known is not None:
            address, source = known
            return self._draft(view, address, source, note=note)
        return self._move(outreach_id, S.NEEDS_EMAIL, note=note, outreach=outreach)

    def forget_founder(self, founder_id: int) -> None:
        """Hard-delete the founder and everything about them (UK GDPR erasure).

        In the same transaction, keyed hashes of their LinkedIn slug, signal id and
        known emails are kept (and nothing else), so a later Evertrace signal about
        the same person is skipped instead of re-creating and re-drafting them.
        """
        try:
            founder = self._store.get_founder(founder_id)
        except NotFoundError:
            raise NotFound("That founder no longer exists") from None
        emails: list[str | None] = [founder.evertrace_email]
        outreach = self._store.get_outreach_for_founder(founder_id)
        if outreach is not None:
            emails.append(outreach.email)
        lookup = self._store.get_email_lookup(founder_id, EmailSource.ROCKETREACH)
        if lookup is not None:
            emails.append(lookup.email)
            emails.extend(c.email for c in lookup.candidates)
        keys = self._suppression.for_identity(
            signal_id=founder.evertrace_signal_id, linkedin_slug=founder.linkedin_slug, emails=emails
        )
        try:
            self._store.erase_founder(founder_id, suppress=keys, at=self._clock())
        except NotFoundError:
            raise NotFound("That founder no longer exists") from None
        logger.info("Founder %s erased (suppressed against re-ingestion)", founder_id)

    def forget_outreach_founder(self, outreach_id: int) -> None:
        """Convenience for the dashboard, which works in outreach ids."""
        founder_id = self._view(outreach_id).founder.id
        if founder_id is None:  # pragma: no cover - stored founders always have ids
            raise NotFound("That founder no longer exists")
        self.forget_founder(founder_id)

    def rerender_open_drafts(self) -> int:
        """Re-render every DRAFTED outreach with the current template (after an edit).
        Not a status change, so no audit event. Returns how many changed."""
        template = self._store.get_template()
        changed = 0
        for view in self._store.list_outreach(frozenset({S.DRAFTED}), limit=MAX_LIST):
            outreach = view.outreach
            if not outreach.email or outreach.template_version == template.version:
                continue
            try:
                draft = build_draft(template, view.founder, outreach.email)
            except TemplateError:
                logger.warning("Outreach %s draft could not be re-rendered", outreach.id)
                continue
            write_draft(outreach, draft, template)
            outreach.updated_at = self._clock()
            self._store.save_outreach(outreach)
            changed += 1
        return changed

    # ---------------------------------------------------------------- helpers

    def _view(self, outreach_id: int) -> OutreachView:
        try:
            return self._store.get_outreach_view(outreach_id)
        except NotFoundError:
            raise NotFound("That outreach record no longer exists") from None

    def _move(
        self,
        outreach_id: int,
        target: OutreachStatus,
        *,
        note: str | None = None,
        close_reason: CloseReason | None = None,
        outreach: OutreachRecord | None = None,
    ) -> OutreachRecord:
        record = outreach or self._view(outreach_id).outreach
        try:
            event = transition(
                record,
                target,
                source=EventSource.MANUAL,
                at=self._clock(),
                note=note,
                close_reason=close_reason,
            )
        except InvalidTransition as exc:
            raise InvalidAction(f"Cannot move from {_label(exc.current)} to {_label(exc.target)}") from None
        return self._save(record, event)

    def _draft(self, view: OutreachView, address: str, source: EmailSource, *, note: str) -> OutreachRecord:
        outreach = view.outreach
        try:
            event = draft_outreach(
                outreach,
                view.founder,
                address,
                email_source=source,
                template=self._store.get_template(),
                source=EventSource.MANUAL,
                at=self._clock(),
                note=note,
            )
        except InvalidTransition as exc:
            raise InvalidAction(f"Cannot draft from {_label(exc.current)}") from None
        except TemplateError:
            raise ValidationFailed(["That email address is not valid"]) from None
        return self._save(outreach, event)

    def _save(self, outreach: OutreachRecord, event: StatusEvent) -> OutreachRecord:
        try:
            return self._store.save_outreach(outreach, event)
        except NotFoundError:
            raise NotFound("That outreach record no longer exists") from None

    def _known_email(self, founder: FounderRecord) -> tuple[str, EmailSource] | None:
        if founder.evertrace_email:
            return founder.evertrace_email, EmailSource.EVERTRACE
        if founder.id is None:  # pragma: no cover
            return None
        lookup = self._store.get_email_lookup(founder.id, EmailSource.ROCKETREACH)
        if lookup is not None and lookup.outcome is LookupOutcome.FOUND and lookup.email:
            return lookup.email, EmailSource.ROCKETREACH
        return None

    def _email_options(self, founder: FounderRecord) -> tuple[EmailCandidate, ...]:
        if founder.id is None:  # pragma: no cover - stored founders always have ids
            return ()
        lookup = self._store.get_email_lookup(founder.id, EmailSource.ROCKETREACH)
        return lookup.candidates if lookup is not None else ()

    def _item(self, view: OutreachView, now: datetime, lookup: EmailLookupRecord | None) -> OutreachItem:
        o = view.outreach
        since = {S.DRAFTED: o.drafted_at, S.SENT: o.sent_at, S.REPLIED: o.replied_at}.get(o.status)
        days = whole_days_between(since, now) if since is not None else None
        due = o.status is S.SENT and o.sent_at is not None and now - o.sent_at >= self._follow_up
        return OutreachItem(
            view=view,
            days_waiting=days,
            follow_up_due=due,
            email_options=lookup.candidates if lookup is not None else (),
        )

    def _remaining_credits(self, now: datetime) -> int | None:
        """RocketReach credits, cached for a few minutes so the overview page does not
        make an API call on every load. Never raises."""
        if self._finder is None:
            return None
        fresh = self._credits.fetched_at is not None and now - self._credits.fetched_at < timedelta(
            seconds=CREDITS_CACHE_SECONDS
        )
        if not fresh:
            self._credits = _CreditsCache(value=_guarded(self._finder.remaining_credits), fetched_at=now)
        return self._credits.value


def _label(status: OutreachStatus) -> str:
    return status.value.replace("_", " ")


def _reason_label(reason: ExclusionReason | None) -> str:
    return reason.value.replace("_", " ") if reason else "no reason recorded"


def _guarded(fn: Callable[[], int | None]) -> int | None:
    try:
        return fn()
    except Exception as exc:
        logger.warning("RocketReach credit check failed (%s)", type(exc).__name__)
        return None
