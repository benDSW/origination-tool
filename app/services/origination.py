"""The daily origination run (R1-R11): signals -> founders -> eligibility -> dedupe
-> email -> draft -> tracking.

Design rules this module keeps:

* **Idempotent.** A founder is matched by Evertrace signal id OR LinkedIn slug and
  has exactly one outreach record; only DISCOVERED / NEEDS_EMAIL (and location-
  excluded founders a more permissive search admits) are ever drafted, so a
  re-run never drafts anyone twice.
* **Never draft without the dedupe check.** If Affinity is unavailable, founders
  stay DISCOVERED and are retried on the next run.
* **Never lose a signal.** A search's watermark only advances past signals that
  were fully processed, and never on a dry run.
* **One bad founder or search never aborts the run.** Errors are collected as
  short, secret-free strings on the run record.
* **Paid lookups are reserved before they are made.** Each RocketReach call first
  claims the founder's cache row and one unit of today's budget in the database,
  so neither a failure after the call nor a concurrent run can exceed the daily
  cap or look anyone up twice.
* **The run lock has an owner and a heartbeat.** A run that loses its lock (for
  example after a long backfill) stops instead of running alongside another one,
  and it can never release a lock it no longer holds.
* **Erased founders stay erased.** Signals for a forgotten founder are skipped
  (matched by keyed hashes; see :mod:`app.services.suppression`).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.eligibility import evaluate_location
from app.domain.matching import KnownIndex, find_crm_match
from app.domain.models import (
    CompanyRef,
    EmailLookupRecord,
    EmailLookupResult,
    EmailSource,
    EmailTemplate,
    EventSource,
    ExclusionReason,
    FounderRecord,
    LookupOutcome,
    OutreachRecord,
    OutreachStatus,
    OutreachView,
    RunCounts,
    RunRecord,
    RunState,
    RunTrigger,
    SearchConfig,
    Signal,
)
from app.domain.ports import (
    CrmGateway,
    EmailFinder,
    IntegrationError,
    LookupReservation,
    NotFoundError,
    SearchCatalog,
    SignalSource,
    Store,
)
from app.domain.status import transition
from app.domain.templating import normalise_email
from app.services.clock import Clock, start_of_utc_day, utc_now
from app.services.drafting import draft_outreach
from app.services.searches import sync_catalog
from app.services.suppression import SuppressionKeys
from app.services.tracking import TrackingResult, TrackingService

logger = logging.getLogger(__name__)

S = OutreachStatus
RUN_LOCK = "origination-run"
MAX_BATCH = 10_000  # upper bound on outreach rows read in one step
MAX_STORED_ERRORS = 50  # the run row keeps the first N messages; counts.errors has the total
MAX_CONSECUTIVE_FINDER_ERRORS = 3
MAX_HEARTBEAT_INTERVAL_SECONDS = 60
# Cache row written *before* a paid call; replaced by the real outcome afterwards.
# REJECTED (not ERROR) so a lookup whose result was lost is never paid for twice.
PENDING_LOOKUP_REASON = (
    "Lookup started but its result was not saved; not retried automatically "
    "(to avoid paying twice). Enter the email manually."
)


class RunLockLost(RuntimeError):
    """The run lock expired and another run took it over (or it was removed)."""


class _LockLease:
    """The run lock held by this run: refreshed while the run works, and lost
    loudly (``RunLockLost``) if another run has taken it over."""

    def __init__(self, store: Store, name: str, clock: Clock, ttl_seconds: int) -> None:
        self._store = store
        self._name = name
        self._clock = clock
        self._ttl = ttl_seconds
        self.owner = secrets.token_hex(16)
        self._interval = timedelta(seconds=max(1, min(MAX_HEARTBEAT_INTERVAL_SECONDS, ttl_seconds // 4)))
        self._last = clock()

    def acquire(self) -> bool:
        now = self._clock()
        self._last = now
        return self._store.acquire_owned_lock(self._name, owner=self.owner, now=now, ttl_seconds=self._ttl)

    def heartbeat(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last < self._interval:
            return
        if not self._store.refresh_owned_lock(self._name, owner=self.owner, now=now):
            raise RunLockLost("the run lock was taken over by another run")
        self._last = now

    def release(self) -> None:
        self._store.release_owned_lock(self._name, owner=self.owner)


@dataclass
class _RunContext:
    """Mutable bookkeeping for one run."""

    now: datetime
    dry_run: bool
    counts: RunCounts = field(default_factory=RunCounts)
    errors: list[str] = field(default_factory=list)
    progress: bool = False  # anything useful happened (decides PARTIAL vs FAILED)
    # Outreach ids admitted by an eligible search this run (step d candidates).
    admitted: set[int] = field(default_factory=set)
    # EXCLUDED(location) outreach a more permissive search admits -> search ids that saw them.
    rescued: dict[int, set[int]] = field(default_factory=dict)
    # search id -> (config, new watermark), saved only after dedupe (see _save_watermarks).
    watermarks: dict[int, tuple[SearchConfig, datetime]] = field(default_factory=dict)
    crm_failed: bool = False
    lease: _LockLease | None = None

    def heartbeat(self, *, force: bool = False) -> None:
        if self.lease is not None:
            self.lease.heartbeat(force=force)

    def error(self, message: str) -> None:
        self.counts.errors += 1
        if len(self.errors) < MAX_STORED_ERRORS:
            self.errors.append(message[:300])


class OriginationService:
    def __init__(
        self,
        store: Store,
        catalog: SearchCatalog,
        signals: SignalSource,
        crm: CrmGateway,
        email_finder: EmailFinder | None,
        tracking: TrackingService,
        clock: Clock = utc_now,
        *,
        rocketreach_daily_cap: int = 40,
        lock_ttl_seconds: int = 3600,
        suppression: SuppressionKeys | None = None,
    ) -> None:
        self._store = store
        self._suppression = suppression or SuppressionKeys.development()
        self._catalog = catalog
        self._signals = signals
        self._crm = crm
        self._finder = email_finder
        self._tracking = tracking
        self._clock = clock
        self._daily_cap = max(0, rocketreach_daily_cap)
        self._lock_ttl = lock_ttl_seconds

    # ------------------------------------------------------------------ public

    def run(
        self,
        trigger: RunTrigger,
        *,
        dry_run: bool = False,
        use_rocketreach: bool = True,
        only_search_id: int | None = None,
    ) -> RunRecord:
        """Execute one origination run and return its persisted record.

        If another run holds the lock, a SKIPPED run is recorded and returned
        immediately (R3). A dry run still records founders and drafts in the
        tool's own database, but never advances watermarks, never calls
        RocketReach and skips tracking.
        """
        started = self._clock()
        lease = _LockLease(self._store, RUN_LOCK, self._clock, self._lock_ttl)
        if not lease.acquire():
            logger.info("Origination run skipped: another run is in progress")
            return self._store.add_run(
                RunRecord(
                    trigger=trigger,
                    state=RunState.SKIPPED,
                    dry_run=dry_run,
                    started_at=started,
                    finished_at=started,
                    errors=["Another run was already in progress"],
                )
            )
        try:
            run = self._store.add_run(RunRecord(trigger=trigger, dry_run=dry_run, started_at=started))
            ctx = _RunContext(now=started, dry_run=dry_run, lease=lease)
            try:
                self._execute(ctx, use_rocketreach=use_rocketreach, only_search_id=only_search_id)
                if ctx.counts.errors == 0:
                    state = RunState.SUCCEEDED
                else:
                    state = RunState.PARTIAL if ctx.progress else RunState.FAILED
            except RunLockLost:
                logger.error("Origination run stopped: its run lock was taken over by another run")
                ctx.error("Run stopped: the run lock was lost to another run (no further work was done)")
                state = RunState.FAILED
            except Exception as exc:
                logger.exception("Origination run failed")
                ctx.error(f"Run aborted by an unexpected error ({type(exc).__name__})")
                state = RunState.FAILED
            run.state = state
            run.finished_at = self._clock()
            run.counts = ctx.counts
            run.errors = ctx.errors
            saved = self._store.save_run(run)
            logger.info("Origination run %s finished: %s %s", saved.id, state.value, ctx.counts.model_dump())
            return saved
        finally:
            lease.release()

    def sync_tracking(self) -> TrackingResult | None:
        """Run tracking alone (dashboard button / CLI) under the same lock as a run,
        so the two never move the same outreach concurrently. None if busy."""
        lease = _LockLease(self._store, RUN_LOCK, self._clock, self._lock_ttl)
        if not lease.acquire():
            return None
        try:
            return self._tracking.sync()
        finally:
            lease.release()

    # ------------------------------------------------------------------- steps

    def _execute(self, ctx: _RunContext, *, use_rocketreach: bool, only_search_id: int | None) -> None:
        configs = self._sync_catalog(ctx)
        for config in self._searches_to_run(ctx, configs, only_search_id):
            ctx.heartbeat(force=True)
            self._process_search(ctx, config)
        ctx.heartbeat(force=True)
        self._dedupe_and_draft(ctx)
        ctx.heartbeat(force=True)
        self._save_watermarks(ctx)
        if not ctx.dry_run and use_rocketreach and self._finder is not None:
            self._rocketreach_backlog(ctx, self._finder)
        if not ctx.dry_run:
            ctx.heartbeat(force=True)
            result = self._tracking.sync()
            ctx.counts.tracking_updates += result.updates
            ctx.progress = ctx.progress or result.updates > 0 or result.checked > len(result.errors)
            for message in result.errors:
                ctx.error(message)

    # (b) catalogue ----------------------------------------------------------

    def _sync_catalog(self, ctx: _RunContext) -> list[SearchConfig]:
        try:
            sync_catalog(self._store, self._catalog, ctx.now)
        except IntegrationError as exc:
            ctx.error(f"Search catalogue sync failed, using stored searches: {exc}")
        return self._store.list_search_configs()

    def _searches_to_run(
        self, ctx: _RunContext, configs: list[SearchConfig], only_search_id: int | None
    ) -> list[SearchConfig]:
        if only_search_id is None:
            return [c for c in configs if c.enabled]
        # An explicit request (e.g. backfilling a just-created search) runs even if disabled.
        selected = [c for c in configs if c.id == only_search_id]
        if not selected:
            ctx.error(f"Search {only_search_id} not found")
        return selected

    # (c) signals ------------------------------------------------------------

    def _process_search(self, ctx: _RunContext, config: SearchConfig) -> None:
        if config.id is None:  # pragma: no cover - configs from the store always have ids
            return
        ctx.counts.searches += 1
        label = f"Search '{config.title}'"
        since = config.watermark or (ctx.now - timedelta(days=config.lookback_days))
        latest: datetime | None = None
        earliest_failed: datetime | None = None
        try:
            saved = self._catalog.get_search(config.external_id)
            for signal in self._signals.iter_signals(saved.filters, since):
                ctx.heartbeat()  # outside the per-signal try: a lost lock ends the run
                ctx.counts.signals_seen += 1
                try:
                    self._ingest(ctx, config, signal)
                    ctx.progress = True
                except Exception as exc:
                    logger.exception("Signal %s failed", signal.external_id)
                    ctx.error(f"{label}: signal {signal.external_id} failed ({type(exc).__name__})")
                    if earliest_failed is None or signal.discovered_at < earliest_failed:
                        earliest_failed = signal.discovered_at
                if latest is None or signal.discovered_at > latest:
                    latest = signal.discovered_at
        except NotFoundError:
            ctx.error(f"{label}: no longer exists on Evertrace")
            return
        except IntegrationError as exc:
            ctx.error(f"{label}: {exc}")
            return
        ctx.progress = True
        # Advance only past fully processed signals: a failed signal is re-fetched
        # next run (``since`` is inclusive and re-processing is idempotent).
        new_mark = latest if earliest_failed is None else min(earliest_failed, latest or earliest_failed)
        if new_mark is not None and (config.watermark is None or new_mark > config.watermark):
            ctx.watermarks[config.id] = (config, new_mark)

    def _ingest(self, ctx: _RunContext, config: SearchConfig, signal: Signal) -> None:
        if self._is_suppressed(signal):
            # Erased on request: nothing is stored, drafted or looked up again.
            logger.info("Signal %s skipped: the founder was erased from the tool", signal.external_id)
            return
        founder, outreach = self._upsert_founder(ctx, signal)
        if founder.id is None or outreach.id is None or config.id is None:  # pragma: no cover
            raise ValueError("store returned an unsaved record")
        self._store.link_founder_to_search(founder.id, config.id)

        decision = evaluate_location(signal, config.location_policy)
        if outreach.status is S.DISCOVERED:
            if decision.eligible:
                ctx.admitted.add(outreach.id)
            elif outreach.id not in ctx.admitted:
                # Another search may already have admitted this founder in this run; one
                # admitting search is enough.
                event = transition(
                    outreach,
                    S.EXCLUDED,
                    source=EventSource.PIPELINE,
                    at=ctx.now,
                    note=f"Location policy of '{config.title}'",
                )
                outreach.exclusion_reason = ExclusionReason.LOCATION
                outreach.exclusion_detail = decision.reason
                self._store.save_outreach(outreach, event)
                ctx.counts.excluded_location += 1
        elif (
            outreach.status is S.EXCLUDED
            and outreach.exclusion_reason is ExclusionReason.LOCATION
            and decision.eligible
        ):
            ctx.rescued.setdefault(outreach.id, set()).add(config.id)

    def _is_suppressed(self, signal: Signal) -> bool:
        keys = self._suppression.for_identity(
            signal_id=signal.external_id, linkedin_slug=signal.linkedin_slug, emails=[signal.email]
        )
        return self._store.is_suppressed(keys)

    def _upsert_founder(self, ctx: _RunContext, signal: Signal) -> tuple[FounderRecord, OutreachRecord]:
        founder = self._store.find_founder(
            evertrace_signal_id=signal.external_id, linkedin_slug=signal.linkedin_slug
        )
        if founder is None:
            founder = self._store.add_founder(_founder_from_signal(signal, ctx.now))
            ctx.counts.founders_new += 1
        if founder.id is None:  # pragma: no cover
            raise ValueError("store returned an unsaved founder")
        outreach = self._store.get_outreach_for_founder(founder.id)
        if outreach is None:
            # Also heals a founder whose outreach insert failed on an earlier run.
            outreach = self._store.add_outreach(
                OutreachRecord(founder_id=founder.id, created_at=ctx.now, updated_at=ctx.now)
            )
        return founder, outreach

    # (d) Affinity dedupe + draft -------------------------------------------

    def _dedupe_and_draft(self, ctx: _RunContext) -> None:
        candidates: list[OutreachView] = self._store.list_outreach(frozenset({S.DISCOVERED}), limit=MAX_BATCH)
        candidate_ids = {v.outreach.id for v in candidates}
        for outreach_id in sorted(ctx.rescued):
            if outreach_id not in candidate_ids:
                candidates.append(self._store.get_outreach_view(outreach_id))
        if not candidates:
            return
        try:
            index = KnownIndex.build(self._crm.known_entities())
        except IntegrationError as exc:
            ctx.crm_failed = True
            ctx.error(
                f"Affinity dedupe unavailable; {len(candidates)} founder(s) left for the next run: {exc}"
            )
            return
        template = self._store.get_template()
        for view in candidates:
            ctx.heartbeat()
            try:
                self._dedupe_one(ctx, view, index, template)
                ctx.progress = True
            except Exception as exc:
                logger.exception("Dedupe/draft of outreach %s failed", view.outreach.id)
                ctx.error(f"Outreach {view.outreach.id}: draft step failed ({type(exc).__name__})")

    def _dedupe_one(
        self, ctx: _RunContext, view: OutreachView, index: KnownIndex, template: EmailTemplate
    ) -> None:
        outreach, founder = view.outreach, view.founder
        rescued = outreach.status is S.EXCLUDED
        company = (
            CompanyRef(name=founder.company_name or "", website_domain=founder.company_domain)
            if founder.company_name or founder.company_domain
            else None
        )
        match = find_crm_match(index, company, founder.full_name)
        if match is not None:
            if rescued:
                # Still excluded, now for a stronger reason; not a status change, so no event.
                outreach.exclusion_reason = ExclusionReason.IN_AFFINITY
                outreach.exclusion_detail = match.describe()
                outreach.updated_at = ctx.now
                self._store.save_outreach(outreach)
            else:
                event = transition(outreach, S.EXCLUDED, source=EventSource.PIPELINE, at=ctx.now)
                outreach.exclusion_reason = ExclusionReason.IN_AFFINITY
                outreach.exclusion_detail = match.describe()
                self._store.save_outreach(outreach, event)
            ctx.counts.excluded_affinity += 1
            return

        note = "Admitted by a search allowing this location" if rescued else None
        if founder.evertrace_email:
            event = draft_outreach(
                outreach,
                founder,
                founder.evertrace_email,
                email_source=EmailSource.EVERTRACE,
                template=template,
                source=EventSource.PIPELINE,
                at=ctx.now,
                note=note,
            )
            self._store.save_outreach(outreach, event)
            ctx.counts.drafted += 1
        else:
            event = transition(outreach, S.NEEDS_EMAIL, source=EventSource.PIPELINE, at=ctx.now, note=note)
            self._store.save_outreach(outreach, event)
            ctx.counts.needs_email += 1

    def _save_watermarks(self, ctx: _RunContext) -> None:
        if ctx.dry_run:
            return
        # A rescued founder left EXCLUDED because Affinity was down would never be
        # seen again once the watermark moves, so hold back the searches that saw one.
        held = {sid for sids in ctx.rescued.values() for sid in sids} if ctx.crm_failed else set()
        for search_id, (config, mark) in ctx.watermarks.items():
            if search_id in held:
                continue
            try:
                current = self._store.get_search_config(search_id)
                current.watermark = mark
                self._store.save_search_config(current)
            except Exception as exc:
                logger.exception("Saving watermark for search %s failed", search_id)
                ctx.error(f"Search '{config.title}': watermark not saved ({type(exc).__name__})")

    # (e) RocketReach backlog -------------------------------------------------

    def _rocketreach_backlog(self, ctx: _RunContext, finder: EmailFinder) -> None:
        day_start = start_of_utc_day(ctx.now)
        used_today = max(
            self._store.count_email_lookups_since(EmailSource.ROCKETREACH, day_start),
            self._store.lookups_reserved_since(EmailSource.ROCKETREACH, day_start),
        )
        if used_today >= self._daily_cap:
            logger.info("RocketReach daily cap (%d) already reached", self._daily_cap)
            return
        backlog = self._lookup_backlog()
        if not backlog:
            return
        credits = _guarded(finder.remaining_credits)
        if credits is not None and credits <= 0:
            ctx.error("RocketReach credits exhausted; email lookups skipped")
            return

        template = self._store.get_template()
        consecutive_errors = 0
        for view in backlog:
            ctx.heartbeat(force=True)  # never spend a credit without holding the run lock
            try:
                claim = self._reserve_lookup(ctx, view)
            except Exception as exc:
                logger.exception("Reserving a RocketReach lookup for outreach %s failed", view.outreach.id)
                ctx.error(
                    f"RocketReach lookups stopped: a lookup could not be reserved ({type(exc).__name__})"
                )
                break
            if claim == LookupReservation.CAP_REACHED:
                logger.info("RocketReach daily cap (%d) reached", self._daily_cap)
                break
            if claim == LookupReservation.ALREADY_CLAIMED:
                continue  # another run (or an earlier attempt) owns this founder's lookup
            try:
                raised = self._lookup_one(ctx, finder, view, template)
            except Exception as exc:
                # A credit may already be spent and the outcome is unknown: stop paying
                # for lookups in this run rather than moving on to the next founder.
                logger.exception("RocketReach step for outreach %s failed", view.outreach.id)
                ctx.error(
                    f"Outreach {view.outreach.id}: email lookup step failed ({type(exc).__name__}); "
                    "RocketReach lookups stopped for this run"
                )
                break
            if raised is None:
                consecutive_errors = 0
                continue
            consecutive_errors += 1
            if not raised.retryable:
                ctx.error("RocketReach lookups stopped for this run (non-retryable error)")
                break
            if consecutive_errors >= MAX_CONSECUTIVE_FINDER_ERRORS:
                ctx.error(
                    f"RocketReach lookups stopped for this run after {consecutive_errors} consecutive errors"
                )
                break

    def _reserve_lookup(self, ctx: _RunContext, view: OutreachView) -> str:
        """Persist the attempt (and take today's budget) before any credit is spent."""
        if view.founder.id is None:  # pragma: no cover - filtered by _lookup_backlog
            raise ValueError("founder not eligible for lookup")
        reservation = EmailLookupRecord(
            founder_id=view.founder.id,
            provider=EmailSource.ROCKETREACH,
            outcome=LookupOutcome.REJECTED,
            reason=PENDING_LOOKUP_REASON,
            created_at=ctx.now,
        )
        return self._store.reserve_email_lookup(
            reservation, day_start=start_of_utc_day(ctx.now), daily_cap=self._daily_cap
        )

    def _lookup_backlog(self) -> list[OutreachView]:
        """NEEDS_EMAIL founders with a LinkedIn slug and no settled lookup, oldest first."""
        views = self._store.list_outreach(frozenset({S.NEEDS_EMAIL}), limit=MAX_BATCH)
        views.sort(key=lambda v: (v.outreach.created_at, v.outreach.id or 0))
        backlog: list[OutreachView] = []
        for view in views:
            if not view.founder.linkedin_slug or view.founder.id is None:
                continue
            cached = self._store.get_email_lookup(view.founder.id, EmailSource.ROCKETREACH)
            if cached is not None and cached.outcome is not LookupOutcome.ERROR:
                continue  # never look anyone up twice (R10); ERROR rows are retried
            backlog.append(view)
        return backlog

    def _lookup_one(
        self, ctx: _RunContext, finder: EmailFinder, view: OutreachView, template: EmailTemplate
    ) -> IntegrationError | None:
        """Look one founder up, cache the outcome, draft on FOUND. Returns the
        IntegrationError if the provider raised (for the consecutive-error stop)."""
        founder, outreach = view.founder, view.outreach
        if founder.id is None or founder.linkedin_slug is None:  # pragma: no cover - filtered earlier
            raise ValueError("founder not eligible for lookup")
        raised: IntegrationError | None = None
        try:
            result = finder.find_email(
                linkedin_slug=founder.linkedin_slug,
                full_name=founder.full_name,
                company_domain=founder.company_domain,
            )
        except IntegrationError as exc:
            raised = exc
            result = EmailLookupResult(outcome=LookupOutcome.ERROR, reason=str(exc)[:200])
            ctx.error(f"Outreach {outreach.id}: RocketReach lookup failed: {exc}")
        ctx.counts.rocketreach_lookups += 1  # the attempt counts even if recording it fails
        ctx.progress = True
        # Replace the reservation with the outcome (ERROR rows are retried on a later run).
        self._store.add_email_lookup(
            EmailLookupRecord(
                founder_id=founder.id,
                provider=EmailSource.ROCKETREACH,
                outcome=result.outcome,
                email=result.email,
                grade=result.grade,
                reason=result.reason,
                created_at=ctx.now,
            )
        )
        email = normalise_email(result.email) if result.outcome is LookupOutcome.FOUND else None
        if email is not None:
            event = draft_outreach(
                outreach,
                founder,
                email,
                email_source=EmailSource.ROCKETREACH,
                template=template,
                source=EventSource.PIPELINE,
                at=ctx.now,
                note=f"RocketReach email (grade {result.grade or 'n/a'})",
            )
            self._store.save_outreach(outreach, event)
            ctx.counts.drafted += 1
        return raised


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _founder_from_signal(signal: Signal, now: datetime) -> FounderRecord:
    company = signal.company
    return FounderRecord(
        evertrace_signal_id=signal.external_id,
        linkedin_slug=signal.linkedin_slug,
        first_name=signal.first_name,
        last_name=signal.last_name,
        city=signal.city,
        country=signal.country,
        gender=signal.gender,
        score=signal.score,
        signal_type=signal.signal_type,
        summary=signal.summary,
        discovered_at=signal.discovered_at,
        company_name=company.name if company else None,
        company_domain=company.website_domain if company else None,
        company_registry_url=company.registry_url if company else None,
        evertrace_email=normalise_email(signal.email),
        created_at=now,
    )


def _guarded(fn: Callable[[], int | None]) -> int | None:
    """Credit checks must never break a run: unknown is treated as 'carry on'."""
    try:
        return fn()
    except Exception as exc:
        logger.warning("RocketReach credit check failed (%s)", type(exc).__name__)
        return None
