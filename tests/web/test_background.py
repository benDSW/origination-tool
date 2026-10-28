"""The dashboard's automatic reply-tracking timer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.tracking import TrackingResult
from app.web.background import tracking_loop
from app.web.config import WebConfig
from app.web.factory import create_web_app


class _Origination:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def sync_tracking(self) -> TrackingResult | None:
        self.calls += 1
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Services:
    def __init__(self, origination: _Origination) -> None:
        self.origination = origination
        self.clock = lambda: None


async def _run_ticks(services: Any, ticks: int) -> None:
    task = asyncio.create_task(tracking_loop(lambda: services, 0.001, first_delay_seconds=0))
    while (services.origination.calls if services else 0) < ticks:
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_loop_keeps_syncing_through_failures_and_busy_locks(caplog: pytest.LogCaptureFixture) -> None:
    origination = _Origination(
        [
            RuntimeError("synthetic-detail-not-for-logs"),
            None,
            TrackingResult(updates=2, checked=5, errors=["x"]),
            TrackingResult(),
        ]
    )
    caplog.set_level("INFO")
    asyncio.run(_run_ticks(_Services(origination), 4))
    assert origination.calls >= 4
    assert "Automatic tracking sync failed (RuntimeError)" in caplog.text
    assert "synthetic-detail-not-for-logs" not in caplog.text
    assert "skipped: a run is in progress" in caplog.text
    assert "5 checked, 2 update(s), 1 error(s)" in caplog.text


def test_loop_waits_while_services_are_not_built() -> None:
    async def scenario() -> None:
        task = asyncio.create_task(tracking_loop(lambda: None, 0.001, first_delay_seconds=0))
        await asyncio.sleep(0.02)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


class _Auth:
    session_binding = None

    def verify(self, password: str) -> bool:
        return False


def test_app_lifespan_starts_and_stops_the_timer_only_when_configured(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    started: list[float] = []

    async def fake_loop(get_services: Any, interval: float) -> None:
        started.append(interval)
        await asyncio.Event().wait()  # until cancelled at shutdown

    monkeypatch.setattr("app.web.factory.tracking_loop", fake_loop)
    secret = "s" * 40
    services: Any = _Services(_Origination([None]))
    on = create_web_app(
        WebConfig(session_secret=secret, tracking_interval_seconds=3600), _Auth(), services=services
    )
    with TestClient(on):
        pass
    off = create_web_app(WebConfig(session_secret=secret), _Auth(), services=services)
    with TestClient(off):
        pass
    assert started == [3600]
