"""Regression tests for the security review, pipeline side:

1. the RocketReach daily cap holds even when recording a lookup fails;
2. the run lock has an owner and a heartbeat, and paid lookups are race-safe;
5. a forgotten founder is never re-ingested, re-drafted or looked up again;
6. a deployment that would run fail-open refuses to start.

Services run against a real SqlStore (temp SQLite) and fakes. Synthetic data only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from app import main
from app.config import (
    ConfigurationError,
    Settings,
    check_runtime_safety,
    describe_security_mode,
    on_hosting_platform,
)
from app.container import suppression_keys
from app.domain.models import (
    EmailLookupRecord,
    EmailLookupResult,
    EmailSource,
    LookupOutcome,
    OutreachStatus,
    RunState,
    RunTrigger,
    SearchFilter,
    Signal,
)
from app.jobs import _runtime
from app.services import SuppressionKeys
from app.services.origination import PENDING_LOOKUP_REASON, RUN_LOCK
from app.services.sessions import SessionService
from app.services.suppression import MIN_KEY_BYTES
from tests.fakes import FakeEmailFinder, make_signal
from tests.services import conftest as service_fixtures
from tests.services.conftest import NORTH, NOW, World, test_settings

# Service fixtures (tests/security has no conftest of its own).
store = service_fixtures.store
clock = service_fixtures.clock
world = service_fixtures.world

S = OutreachStatus
RR = EmailSource.ROCKETREACH
STRONG = "s" * 40  # synthetic secret value


def _run(world: World, **kwargs: Any):  # noqa: ANN202 - test helper
    return world.services.origination.run(RunTrigger.CRON, **kwargs)


def _feed(world: World, *signals: Signal) -> None:
    world.signals.by_filters[NORTH.filters] = list(signals)


def _needs_email_backlog(world: World, n: int) -> None:
    """``n`` founders parked in NEEDS_EMAIL without spending anything."""
    world.enable(NORTH)
    _feed(world, *[make_signal(i) for i in range(1, n + 1)])
    _run(world, use_rocketreach=False)
    assert world.finder.calls == []


class _Wrapped:
    """Delegates to the real store; subclasses override single methods."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# --------------------------------------------------------------------------- #
# Finding 1: the cap counts attempts, not successfully recorded lookups
# --------------------------------------------------------------------------- #


def test_cap_holds_when_recording_every_lookup_fails(world: World) -> None:
    """The audit's reproduction: 60 NEEDS_EMAIL founders, cap 5, a store whose
    ``add_email_lookup`` always raises. Before the fix one run made 60 calls and a
    second run 60 more."""
    _needs_email_backlog(world, 60)

    class _Broken(_Wrapped):
        def add_email_lookup(self, record: EmailLookupRecord) -> None:
            raise RuntimeError("database went away")

    world.store = _Broken(world.store)  # type: ignore[assignment]
    world.rebuild(rocketreach_daily_cap=5)

    first = _run(world)

    # An unexpected failure after a paid call ends the phase for this run.
    assert world.finder.calls == ["alex-testfounder-1"]
    assert first.counts.rocketreach_lookups == 1
    assert any("RocketReach lookups stopped for this run" in e for e in first.errors)

    for _ in range(9):  # the rest of the day's runs (cron, "Run now" clicks...)
        _run(world)

    assert len(world.finder.calls) == 5  # the cap, not 60 or 120
    assert len(set(world.finder.calls)) == 5  # and nobody twice
    assert world.store.lookups_reserved_since(RR, NOW.replace(hour=0)) == 5
    # The reservations written before each call survive the failed recording.
    reserved = [
        v.founder.id
        for v in world.store.list_outreach(frozenset({S.NEEDS_EMAIL}), limit=100)
        if world.store.get_email_lookup(v.founder.id or 0, RR) is not None
    ]
    assert len(reserved) == 5
    cached = world.store.get_email_lookup(reserved[0] or 0, RR)
    assert cached is not None and cached.outcome is LookupOutcome.REJECTED
    assert cached.reason == PENDING_LOOKUP_REASON


def test_cap_holds_across_runs_even_if_each_run_fails_after_its_call(world: World) -> None:
    """Even if lookups kept failing after the call on every run, total paid calls in
    a day never exceed the cap (each run stops after one, the budget counts it)."""
    _needs_email_backlog(world, 10)

    class _Broken(_Wrapped):
        def add_email_lookup(self, record: EmailLookupRecord) -> None:
            raise RuntimeError("database went away")

    world.store = _Broken(world.store)  # type: ignore[assignment]
    world.rebuild(rocketreach_daily_cap=3)

    for _ in range(6):
        _run(world)
        world.clock.advance(minutes=10)

    assert len(world.finder.calls) == 3
    assert len(set(world.finder.calls)) == 3
    assert world.store.lookups_reserved_since(RR, NOW.replace(hour=0)) == 3


def test_failure_while_drafting_after_a_found_email_stops_lookups(world: World) -> None:
    _needs_email_backlog(world, 3)
    for i in range(1, 4):
        world.finder.results[f"alex-testfounder-{i}"] = EmailLookupResult(
            outcome=LookupOutcome.FOUND, email=f"alex{i}@co{i}.example", grade="A"
        )

    class _Broken(_Wrapped):
        def save_outreach(self, outreach: Any, event: Any = None) -> Any:
            raise RuntimeError("database went away")

    world.store = _Broken(world.store)  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert world.finder.calls == ["alex-testfounder-1"]
    assert any("email lookup step failed (RuntimeError)" in e for e in run.errors)


def test_a_failed_reservation_spends_nothing(world: World) -> None:
    _needs_email_backlog(world, 2)

    class _Broken(_Wrapped):
        def reserve_email_lookup(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("database went away")

    world.store = _Broken(world.store)  # type: ignore[assignment]
    world.rebuild()

    run = _run(world)

    assert world.finder.calls == []
    assert "RocketReach lookups stopped: a lookup could not be reserved (RuntimeError)" in run.errors


# --------------------------------------------------------------------------- #
# Finding 2: owned lock + heartbeat; race-safe reservations
# --------------------------------------------------------------------------- #


class _SlowSignals:
    """A SignalSource whose iteration takes (fake) time; ``between`` runs after each
    signal is processed, as if something else happened meanwhile."""

    def __init__(self, world: World, signals: list[Signal], step: timedelta, between: Any) -> None:
        self.world = world
        self.signals = signals
        self.step = step
        self.between = between
        self.calls: list[tuple[tuple[SearchFilter, ...], datetime]] = []

    def iter_signals(self, filters: tuple[SearchFilter, ...], since: datetime) -> Iterator[Signal]:
        self.calls.append((filters, since))
        for signal in self.signals:
            yield signal
            self.world.clock.advance(seconds=self.step.total_seconds())
            self.between()


def test_heartbeat_keeps_a_long_run_locked(world: World) -> None:
    """A three-signal search that takes 2 hours (TTL 1 hour). Without a heartbeat a
    contender could take the lock after the first hour; with it, it never can."""
    world.enable(NORTH)
    contender_results: list[bool] = []

    def contender() -> None:
        contender_results.append(
            world.store.acquire_owned_lock(RUN_LOCK, owner="cron", now=world.clock(), ttl_seconds=3600)
        )

    world.signals = _SlowSignals(  # type: ignore[assignment]
        world, [make_signal(i) for i in range(1, 4)], timedelta(minutes=40), contender
    )
    world.rebuild()

    run = _run(world)

    assert run.state is RunState.SUCCEEDED
    assert contender_results == [False, False, False]
    # Released at the end by its owner.
    assert world.store.acquire_owned_lock(RUN_LOCK, owner="next", now=world.clock(), ttl_seconds=3600)


def test_run_that_lost_its_lock_stops_and_leaves_the_new_holder_alone(world: World) -> None:
    """The audit's scenario: a backfill outlives the TTL (one slow Evertrace call
    here), the cron run takes the stale lock, and the first run must neither carry
    on (double RocketReach spend) nor release the cron run's lock."""
    world.enable(NORTH)
    stolen: list[bool] = []

    def cron_takes_over() -> None:
        if not stolen:
            stolen.append(
                world.store.acquire_owned_lock(RUN_LOCK, owner="cron", now=world.clock(), ttl_seconds=3600)
            )

    world.signals = _SlowSignals(  # type: ignore[assignment]
        world, [make_signal(1), make_signal(2)], timedelta(hours=2), cron_takes_over
    )
    world.rebuild()

    run = _run(world)

    assert stolen == [True]
    assert run.state is RunState.FAILED
    assert any("run lock was lost" in e for e in run.errors)
    assert world.finder.calls == []  # never reached the paid phase
    assert world.config(NORTH).watermark is None  # nothing half-done is committed
    # The cron run's lock is intact: a third contender ("Run now") is refused.
    assert not world.store.acquire_owned_lock(RUN_LOCK, owner="click", now=world.clock(), ttl_seconds=3600)


def test_founder_claimed_by_a_concurrent_run_is_not_looked_up_again(world: World) -> None:
    """Two runs working through the same backlog snapshot: the second claim of a
    founder fails in the database, so nobody is paid for twice."""
    _needs_email_backlog(world, 2)
    store_ref = world.store

    class _Racing(FakeEmailFinder):
        def find_email(self, *, linkedin_slug: str, full_name: str, company_domain: str | None) -> Any:
            if linkedin_slug == "alex-testfounder-1":
                # Meanwhile another run reserves founder 2.
                other = next(
                    v.founder.id
                    for v in store_ref.list_outreach(frozenset({S.NEEDS_EMAIL}))
                    if v.founder.linkedin_slug == "alex-testfounder-2"
                )
                assert other is not None
                store_ref.reserve_email_lookup(
                    EmailLookupRecord(
                        founder_id=other,
                        provider=RR,
                        outcome=LookupOutcome.REJECTED,
                        reason=PENDING_LOOKUP_REASON,
                        created_at=NOW,
                    ),
                    day_start=NOW.replace(hour=0),
                    daily_cap=40,
                )
            return super().find_email(
                linkedin_slug=linkedin_slug, full_name=full_name, company_domain=company_domain
            )

    finder = _Racing()
    world.rebuild(finder=finder)

    run = _run(world)

    assert finder.calls == ["alex-testfounder-1"]
    assert run.counts.rocketreach_lookups == 1


def test_cap_is_shared_with_a_concurrent_run(world: World) -> None:
    """Another run already spent today's budget after this run's pre-check: the
    reservation refuses, so the cap is never exceeded."""
    _needs_email_backlog(world, 3)
    day = NOW.replace(hour=0)

    class _Late(_Wrapped):
        def lookups_reserved_since(self, provider: EmailSource, day_start: datetime) -> int:
            return 0  # stale pre-check

        def count_email_lookups_since(self, provider: EmailSource, since: datetime) -> int:
            return 0

    # A concurrent run reserves the only unit of budget.
    founder_ids = [v.founder.id or 0 for v in world.store.list_outreach(frozenset({S.NEEDS_EMAIL}))]
    reserved = world.store.reserve_email_lookup(
        EmailLookupRecord(
            founder_id=founder_ids[0], provider=RR, outcome=LookupOutcome.REJECTED, created_at=NOW
        ),
        day_start=day,
        daily_cap=1,
    )
    assert reserved == "reserved"
    world.store = _Late(world.store)  # type: ignore[assignment]
    world.rebuild(rocketreach_daily_cap=1)

    _run(world)

    assert world.finder.calls == []


# --------------------------------------------------------------------------- #
# Finding 5: forgotten founders stay forgotten
# --------------------------------------------------------------------------- #


def _forget(world: World, signal_id: str) -> None:
    founder = world.store.find_founder(evertrace_signal_id=signal_id, linkedin_slug=None)
    assert founder is not None and founder.id is not None
    world.services.outreach.forget_founder(founder.id)


def test_forgotten_founder_is_not_reingested_from_a_new_signal(world: World) -> None:
    """The audit's reproduction: forget, then a new signal (new id, same LinkedIn
    slug) arrives. Before the fix this produced a new draft for the same person."""
    world.enable(NORTH)
    _feed(world, make_signal(1, email="alex1@co1.example"))
    _run(world)
    _forget(world, "sig_test0001")

    world.clock.advance(days=3)
    _feed(world, make_signal(1, external_id="sig_new0001", discovered_at=world.clock(), email=None))
    run = _run(world)

    assert world.store.list_outreach() == []
    assert (
        world.store.find_founder(evertrace_signal_id="sig_new0001", linkedin_slug="alex-testfounder-1")
        is None
    )
    assert run.counts.founders_new == 0 and run.counts.drafted == 0
    assert world.finder.calls == []  # and no paid lookup either


def test_forgotten_founder_is_not_reingested_by_a_backfill_or_by_email(world: World) -> None:
    world.enable(NORTH)
    _feed(world, make_signal(1, linkedin_slug=None, email="Alex1@Co1.example"))
    _run(world)
    _forget(world, "sig_test0001")

    # A backfill re-emits the very same signal (no slug: matched on the signal id).
    _feed(world, make_signal(1, linkedin_slug=None, email="Alex1@Co1.example"))
    world.rebuild()
    world.services.origination.run(RunTrigger.MANUAL)
    assert world.store.list_outreach() == []

    # A different signal and profile, same (case-varied) email address.
    _feed(world, make_signal(2, linkedin_slug="someone-else-2", email="alex1@CO1.example"))
    _run(world)
    assert world.store.list_outreach() == []

    # Unrelated founders are still ingested.
    _feed(world, make_signal(3))
    _run(world)
    assert [v.founder.evertrace_signal_id for v in world.store.list_outreach()] == ["sig_test0003"]


def test_forgetting_suppresses_rocketreach_and_manual_emails_too(world: World) -> None:
    world.enable(NORTH)
    world.finder.results["alex-testfounder-1"] = EmailLookupResult(
        outcome=LookupOutcome.FOUND, email="alex1@co1.example", grade="A"
    )
    _feed(world, make_signal(1))
    _run(world)
    _forget(world, "sig_test0001")

    keys = SuppressionKeys.development().for_identity(emails=["alex1@co1.example"])
    assert world.store.is_suppressed(keys)


def test_suppression_hashes_are_keyed_and_hold_no_identifiers() -> None:
    dev = SuppressionKeys.development()
    other = SuppressionKeys(b"another-synthetic-key-0123456789")
    mine = dev.for_identity(
        signal_id="sig_x", linkedin_slug="Alex-Testfounder/", emails=["A@Co.example", None, "bad"]
    )
    assert len(mine) == 3 and all(len(h) == 64 for h in mine)
    assert dev.for_identity(linkedin_slug="alex-testfounder") == dev.for_identity(
        linkedin_slug=" ALEX-TESTFOUNDER "
    )
    assert set(mine).isdisjoint(other.for_identity(signal_id="sig_x", linkedin_slug="alex-testfounder"))
    assert dev.for_identity() == [] and dev.for_identity(signal_id="  ", linkedin_slug="") == []
    assert "testfounder" not in " ".join(mine) and "key" not in repr(dev)
    with pytest.raises(ValueError):
        SuppressionKeys(b"x" * (MIN_KEY_BYTES - 1))


def test_container_uses_the_configured_suppression_secret() -> None:
    configured = suppression_keys(test_settings(suppression_secret=STRONG))
    assert configured.for_identity(signal_id="sig_x") != SuppressionKeys.development().for_identity(
        signal_id="sig_x"
    )
    assert suppression_keys(test_settings()).for_identity(signal_id="sig_x") == (
        SuppressionKeys.development().for_identity(signal_id="sig_x")
    )
    with pytest.raises(ValueError, match="SUPPRESSION_SECRET"):
        suppression_keys(test_settings(app_env="production"))


# --------------------------------------------------------------------------- #
# Finding 6: fail-closed production configuration
# --------------------------------------------------------------------------- #

PG = "postgresql://app@db.invalid/app"  # never connected to
RAILWAY = {"RAILWAY_ENVIRONMENT": "production"}


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_hosted_service_must_run_in_production_mode() -> None:
    assert on_hosting_platform(RAILWAY) and not on_hosting_platform({})
    with pytest.raises(ConfigurationError, match="APP_ENV"):
        check_runtime_safety(_settings(), RAILWAY)  # APP_ENV forgotten: default is development
    with pytest.raises(ConfigurationError, match="APP_ENV"):
        check_runtime_safety(_settings(app_env="test", database_url=PG), {"RAILWAY_PROJECT_ID": "p"})
    check_runtime_safety(_settings(), {})  # local development is fine


def test_production_refuses_sqlite_and_a_missing_suppression_secret() -> None:
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        check_runtime_safety(_settings(app_env="production", suppression_secret=STRONG), RAILWAY)
    with pytest.raises(ConfigurationError, match="SUPPRESSION_SECRET") as info:
        check_runtime_safety(
            _settings(app_env="production", database_url=PG, suppression_secret="short"), RAILWAY
        )
    assert "short" not in str(info.value).replace("shorter", "")
    check_runtime_safety(_settings(app_env="production", database_url=PG, suppression_secret=STRONG), RAILWAY)


def test_security_mode_line_has_no_values() -> None:
    settings = _settings(
        app_env="production", database_url=PG, session_secret=STRONG, suppression_secret=SecretStr(STRONG)
    )
    line = describe_security_mode(settings)
    assert "secure_cookies=on" in line and "hsts=on" in line and "db_backend=postgresql" in line
    assert "session_secret=set" in line and STRONG not in line and "db.invalid" not in line
    assert "secure_cookies=off" in describe_security_mode(_settings())
    assert "suppression_secret=missing" in describe_security_mode(_settings())


def test_entry_points_refuse_to_start_misconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    dev = _settings()  # as if APP_ENV and DATABASE_URL had been forgotten
    built: list[str] = []
    for module in (_runtime, main):
        monkeypatch.setattr(module, "get_settings", lambda: dev)
        monkeypatch.setattr(module, "configure_logging", lambda secrets: None)
    monkeypatch.setattr(_runtime, "build_services", lambda s: built.append("services"))
    monkeypatch.setattr(main, "create_app", lambda settings: built.append("app"))

    with pytest.raises(ConfigurationError):
        _runtime.start()
    with pytest.raises(ConfigurationError):
        main._default_app()
    assert built == []  # nothing was built, nothing touched the network


def test_entry_points_log_the_security_mode(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    for marker in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"):
        monkeypatch.delenv(marker, raising=False)
    dev = _settings()
    for module in (_runtime, main):
        monkeypatch.setattr(module, "get_settings", lambda: dev)
        monkeypatch.setattr(module, "configure_logging", lambda secrets: None)
    monkeypatch.setattr(_runtime, "build_services", lambda s: "services")
    monkeypatch.setattr(main, "create_app", lambda settings: "app")
    for name in ("app.jobs._runtime", "app.main"):
        __import__("logging").getLogger(name).disabled = False
    with caplog.at_level("INFO"):
        assert _runtime.start() == (dev, "services")
        assert main._default_app() == "app"
    assert "Job security mode: app_env=development" in caplog.text
    assert "Dashboard security mode: app_env=development" in caplog.text


# --------------------------------------------------------------------------- #
# Finding 7 (service side): sessions are revocable server-side
# --------------------------------------------------------------------------- #


def test_session_service_stores_only_a_digest_and_revokes(world: World) -> None:
    sessions = world.services.sessions
    assert isinstance(sessions, SessionService)
    sid = sessions.start(3600)
    assert sessions.is_active(sid)
    assert not world.store.admin_session_active(sid, now=NOW)  # the raw id is never stored
    for bad in (None, "", 42, "x" * 500, "unknown"):
        assert not sessions.is_active(bad)
        sessions.end(bad)  # ignored
    other = sessions.start(3600)
    sessions.end(sid)
    assert not sessions.is_active(sid) and sessions.is_active(other)
    assert sessions.end_all() == 1 and not sessions.is_active(other)
    expiring = sessions.start(60)
    world.clock.advance(seconds=61)
    assert not sessions.is_active(expiring)


def test_forget_reports_a_concurrent_erasure_as_not_found(world: World) -> None:
    from app.domain.ports import NotFoundError
    from app.services import NotFound

    world.enable(NORTH)
    _feed(world, make_signal(1))
    _run(world, use_rocketreach=False)

    class _Raced(_Wrapped):
        def erase_founder(self, *args: Any, **kwargs: Any) -> None:
            raise NotFoundError("founder 1 not found")  # deleted by another request meanwhile

    world.store = _Raced(world.store)  # type: ignore[assignment]
    world.rebuild()
    with pytest.raises(NotFound):
        _forget(world, "sig_test0001")
