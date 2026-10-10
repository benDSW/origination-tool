"""Service-test fixtures: a real SqlStore on a temp SQLite file, fakes for every
integration port, and a controllable clock. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.container import build_services_from
from app.domain.models import LocationPolicy, SavedSearch, SearchConfig, SearchFilter
from app.persistence import SqlStore, create_all, make_engine, make_session_factory
from app.services import Services
from tests.fakes import (
    FakeCrm,
    FakeEmailFinder,
    FakeInbox,
    FakeSearchCatalog,
    FakeSignalSource,
    utc,
)

NOW = utc(2026, 10, 9, 6, 0)

# Two synthetic saved searches. Titles sort A before B (the store lists by title).
NORTH = SavedSearch(
    external_id="sch_north",
    title="A North founders",
    filters=(SearchFilter(key="country", values=("United Kingdom",)),),
)
LONDON = SavedSearch(
    external_id="sch_london",
    title="B Female founders - London",
    filters=(SearchFilter(key="gender", values=("woman",)),),
)


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def test_settings(**overrides: object) -> Settings:
    """Settings that never read the developer's .env file."""
    values: dict[str, object] = {"database_url": "sqlite://", "rocketreach_daily_cap": 40}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


test_settings.__test__ = False  # type: ignore[attr-defined]  # not a pytest test


@dataclass
class World:
    store: SqlStore
    catalog: FakeSearchCatalog
    signals: FakeSignalSource
    crm: FakeCrm
    finder: FakeEmailFinder
    inbox: FakeInbox
    clock: FakeClock
    services: Services

    def rebuild(self, *, finder: FakeEmailFinder | None | bool = True, **settings: object) -> Services:
        """Re-wire services (e.g. a different daily cap, or no email finder)."""
        chosen = self.finder if finder is True else (None if finder in (None, False) else finder)
        self.services = build_services_from(
            store=self.store,
            catalog=self.catalog,
            signals=self.signals,
            crm=self.crm,
            email_finder=chosen,  # type: ignore[arg-type]
            inbox=self.inbox,
            settings=test_settings(**settings),
            clock=self.clock,
        )
        return self.services

    def enable(
        self,
        saved: SavedSearch,
        *,
        policy: LocationPolicy = LocationPolicy.UK_OUTSIDE_LONDON,
        lookback_days: int = 30,
        enabled: bool = True,
    ) -> SearchConfig:
        if saved not in self.catalog.searches:
            self.catalog.searches.append(saved)
        config = self.store.upsert_search_from_catalog(saved, synced_at=self.clock())
        config.enabled = enabled
        config.location_policy = policy
        config.lookback_days = lookback_days
        return self.store.save_search_config(config)

    def config(self, saved: SavedSearch) -> SearchConfig:
        return next(c for c in self.store.list_search_configs() if c.external_id == saved.external_id)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqlStore]:
    engine = make_engine("sqlite:///" + (tmp_path / "services.db").as_posix())
    create_all(engine)
    yield SqlStore(make_session_factory(engine))
    engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world(store: SqlStore, clock: FakeClock) -> World:
    w = World(
        store=store,
        catalog=FakeSearchCatalog(),
        signals=FakeSignalSource(),
        crm=FakeCrm(),
        finder=FakeEmailFinder(),
        inbox=FakeInbox(),
        clock=clock,
        services=None,  # type: ignore[arg-type]
    )
    w.rebuild()
    return w
