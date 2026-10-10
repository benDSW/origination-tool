"""CLI jobs: main(argv) with the container patched to fakes + a temp SQLite store."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from app.domain.models import LocationPolicy, RunState, RunTrigger, Signal
from app.jobs import _runtime, daily, hash_password, searches, track
from app.persistence import SqlStore, create_all, make_engine, make_session_factory
from app.services.origination import RUN_LOCK
from tests.fakes import (
    FakeCrm,
    FakeEmailFinder,
    FakeInbox,
    FakeSearchCatalog,
    FakeSignalSource,
    make_signal,
)
from tests.services.conftest import LONDON, NORTH, NOW, FakeClock, World, test_settings


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    engine = make_engine("sqlite:///" + (tmp_path / "jobs.db").as_posix())
    create_all(engine)
    w = World(
        store=SqlStore(make_session_factory(engine)),
        catalog=FakeSearchCatalog(),
        signals=FakeSignalSource(),
        crm=FakeCrm(),
        finder=FakeEmailFinder(),
        inbox=FakeInbox(),
        clock=FakeClock(),
        services=None,  # type: ignore[arg-type]
    )
    w.rebuild()
    closed: list[bool] = []
    w.services.closers.append(lambda: closed.append(True))
    monkeypatch.setattr(_runtime, "start", lambda: (test_settings(), w.services))
    yield w
    engine.dispose()


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #
# daily
# --------------------------------------------------------------------------- #


def test_daily_runs_and_prints_a_summary(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.enable(NORTH)
    world.signals.by_filters[NORTH.filters] = [make_signal(1, email="a@co1.example"), make_signal(2)]

    code = daily.main([])

    out = _json(capsys)
    assert code == 0
    assert out["state"] == "succeeded"
    assert out["trigger"] == "cron"
    assert out["dry_run"] is False
    assert out["counts"]["drafted"] == 1  # type: ignore[index]
    assert out["counts"]["rocketreach_lookups"] == 1  # type: ignore[index]
    assert out["errors"] == []
    assert world.services.closers == []  # services closed


def test_daily_dry_run_and_no_rocketreach_flags(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.enable(NORTH)
    world.signals.by_filters[NORTH.filters] = [make_signal(1)]

    assert daily.main(["--dry-run", "--no-rocketreach", "--manual"]) == 0

    out = _json(capsys)
    assert out["dry_run"] is True
    assert out["trigger"] == "manual"
    assert world.finder.calls == []
    assert world.config(NORTH).watermark is None


def test_daily_no_rocketreach_only(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.enable(NORTH)
    world.signals.by_filters[NORTH.filters] = [make_signal(1)]
    assert daily.main(["--no-rocketreach"]) == 0
    assert world.finder.calls == []
    assert world.config(NORTH).watermark is not None


def test_daily_single_search_by_external_id(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.enable(NORTH)
    world.catalog.searches.append(LONDON)  # on Evertrace, not yet synced locally
    world.signals.by_filters[LONDON.filters] = [make_signal(5, city="London", email="e@co5.example")]
    world.signals.by_filters[NORTH.filters] = [make_signal(6)]

    assert daily.main(["--search", "sch_london"]) == 0

    assert [c[0] for c in world.signals.calls] == [LONDON.filters]
    assert _json(capsys)["counts"]["searches"] == 1  # type: ignore[index]


def test_daily_unknown_search_exits_2(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.catalog.fail = True  # sync fails too; still a clean exit code
    assert daily.main(["--search", "sch_nope"]) == 2
    assert _json(capsys)["state"] == "error"


def test_daily_failed_run_exits_1(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.enable(NORTH)

    class _Exploding:
        def iter_signals(self, filters, since) -> Iterator[Signal]:  # noqa: ANN001
            raise RuntimeError("boom")

    world.signals = _Exploding()  # type: ignore[assignment]
    closers = world.services.closers
    world.rebuild()
    world.services.closers.extend(closers)

    assert daily.main([]) == 1
    assert _json(capsys)["state"] == "failed"


def test_daily_skipped_run_exits_0(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)
    assert daily.main([]) == 0
    assert _json(capsys)["state"] == "skipped"


def test_summarise_unfinished_run() -> None:
    from app.domain.models import RunRecord

    out = daily.summarise(RunRecord(trigger=RunTrigger.CRON, started_at=NOW))
    assert out["finished_at"] is None
    assert out["state"] == RunState.RUNNING.value


# --------------------------------------------------------------------------- #
# track
# --------------------------------------------------------------------------- #


def test_track_reports_updates(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    from app.domain.models import InboxEvent, InboxEventKind

    world.enable(NORTH)
    world.signals.by_filters[NORTH.filters] = [make_signal(1, email="a@co1.example")]
    world.services.origination.run(RunTrigger.CRON, dry_run=True)
    capsys.readouterr()
    world.inbox.events["a@co1.example"] = [InboxEvent(kind=InboxEventKind.SENT, occurred_at=NOW)]

    assert track.main([]) == 0
    out = _json(capsys)
    assert (out["state"], out["updates"], out["checked"]) == ("succeeded", 1, 1)


def test_track_errors_exit_1(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.enable(NORTH)
    world.signals.by_filters[NORTH.filters] = [make_signal(1, email="a@co1.example")]
    world.services.origination.run(RunTrigger.CRON, dry_run=True)
    world.inbox.fail = True
    assert track.main([]) == 1
    assert _json(capsys)["state"] == "partial"


def test_track_skipped_when_locked(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.store.acquire_owned_lock(RUN_LOCK, owner="other-holder", now=NOW, ttl_seconds=3600)
    assert track.main([]) == 0
    assert _json(capsys)["state"] == "skipped"


# --------------------------------------------------------------------------- #
# searches
# --------------------------------------------------------------------------- #


def test_searches_list_empty(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    assert searches.main(["list"]) == 0
    assert "No searches yet" in capsys.readouterr().out


def test_searches_sync_enable_policy_lookback(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.catalog.searches.extend([NORTH, LONDON])

    assert searches.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "sch_north" in out and "sch_london" in out

    north_id = str(world.config(NORTH).id)
    assert searches.main(["enable", north_id]) == 0
    assert world.config(NORTH).enabled is True
    assert searches.main(["disable", "sch_north"]) == 0
    assert world.config(NORTH).enabled is False
    assert searches.main(["policy", "sch_london", "london_allowed"]) == 0
    assert world.config(LONDON).location_policy is LocationPolicy.LONDON_ALLOWED
    assert searches.main(["lookback", north_id, "60"]) == 0
    assert world.config(NORTH).lookback_days == 60

    config = world.config(NORTH)
    config.watermark = NOW
    world.store.save_search_config(config)
    capsys.readouterr()
    assert searches.main(["list"]) == 0
    assert "2026-10-09 06:00 UTC" in capsys.readouterr().out


def test_searches_errors(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.catalog.searches.append(NORTH)
    searches.main(["sync"])
    capsys.readouterr()

    assert searches.main(["enable", "sch_missing"]) == 2
    assert "No search with that id" in capsys.readouterr().err
    assert searches.main(["lookback", "sch_north", "0"]) == 1
    assert "Lookback must be" in capsys.readouterr().err
    assert searches.main(["enable", "999"]) == 1
    with pytest.raises(SystemExit):
        searches.main(["policy", "sch_north", "everywhere"])


# --------------------------------------------------------------------------- #
# hash_password + runtime
# --------------------------------------------------------------------------- #


def _prompts(*answers: str):  # noqa: ANN202
    it = iter(answers)
    return lambda _label: next(it)


def test_hash_password_prints_only_a_verifiable_hash(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "correct horse battery"
    assert hash_password.main([], prompt=_prompts(secret, secret)) == 0
    captured = capsys.readouterr()
    hashed = captured.out.strip()
    assert hashed.startswith("$argon2")
    assert PasswordHasher().verify(hashed, secret)
    assert secret not in captured.out + captured.err


@pytest.mark.parametrize(
    ("first", "second", "message"), [("short", "short", "at least"), ("a" * 12, "b" * 12, "match")]
)
def test_hash_password_rejects(
    capsys: pytest.CaptureFixture[str], first: str, second: str, message: str
) -> None:
    assert hash_password.main([], prompt=_prompts(first, second)) == 1
    captured = capsys.readouterr()
    assert message in captured.err
    assert captured.out == ""
    assert first not in captured.err


def test_runtime_start_configures_logging_before_building(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    settings = test_settings(evertrace_api_key="synthetic-key-123")
    monkeypatch.setattr(_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(
        _runtime, "configure_logging", lambda secrets: order.append(f"logging:{sorted(secrets)}")
    )
    monkeypatch.setattr(_runtime, "build_services", lambda s: order.append("build") or "services")

    assert _runtime.start() == (settings, "services")
    assert order == ["logging:['synthetic-key-123']", "build"]


def test_emit_writes_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    _runtime.emit({"b": 1, "a": NOW})
    assert capsys.readouterr().out == '{"a": "2026-10-09 06:00:00+00:00", "b": 1}\n'
