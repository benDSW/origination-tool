"""Composition root: the real adapters wire up (no network calls are made)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app import container
from app.domain.ports import CrmGateway, IntegrationError
from app.integrations.affinity import AffinityAdapter
from app.integrations.rocketreach import RocketReachAdapter
from app.persistence import create_all, make_engine
from app.services import Services
from tests.services.conftest import test_settings

_RealClient = httpx.Client


class _FastClient(_RealClient):
    """Skips CA-bundle loading (seconds per client on this machine); never used for I/O."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["verify"] = False
        super().__init__(*args, **kwargs)


@pytest.fixture(autouse=True)
def _fast_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "Client", _FastClient)


def _db(tmp_path: Path) -> str:
    url = "sqlite:///" + (tmp_path / "c.db").as_posix()
    engine = make_engine(url)
    create_all(engine)
    engine.dispose()
    return url


def test_build_services_with_all_keys(tmp_path: Path) -> None:
    settings = test_settings(
        database_url=_db(tmp_path),
        evertrace_api_key="synthetic-evertrace-key",
        rocketreach_api_key="synthetic-rocketreach-key",
        affinity_api_key="synthetic-affinity-key",
        rocketreach_daily_cap=7,
        follow_up_after_days=10,
    )

    services = container.build_services(settings)
    try:
        assert isinstance(services, Services)
        assert isinstance(services.origination._finder, RocketReachAdapter)
        assert isinstance(services.origination._crm, AffinityAdapter)
        assert services.origination._daily_cap == 7
        assert services.outreach._follow_up_days == 10
        assert len(services.closers) == 4
        # The store works end to end through the services.
        assert services.templates.current().version == 1
        assert services.searches.list_configs() == []
    finally:
        services.close()
    assert services.closers == []


def test_build_services_without_optional_keys(tmp_path: Path) -> None:
    services = container.build_services(test_settings(database_url=_db(tmp_path)))
    try:
        assert services.origination._finder is None
        assert services.outreach.dashboard_summary().rocketreach_credits is None
        crm = services.origination._crm
        assert isinstance(crm, container.UnconfiguredCrm)
        assert isinstance(crm, CrmGateway)
        with pytest.raises(IntegrationError, match="not configured"):
            crm.known_entities()
        with pytest.raises(IntegrationError):
            crm.email_interactions("a@co.example", datetime(2026, 1, 1))
    finally:
        services.close()


def test_close_tolerates_failing_closers() -> None:
    calls: list[str] = []

    def bad() -> None:
        raise RuntimeError("boom")

    services = Services(
        origination=None,  # type: ignore[arg-type]
        tracking=None,  # type: ignore[arg-type]
        outreach=None,  # type: ignore[arg-type]
        templates=None,  # type: ignore[arg-type]
        searches=None,  # type: ignore[arg-type]
        closers=[lambda: calls.append("first"), bad],
    )
    services.close()
    assert calls == ["first"]
