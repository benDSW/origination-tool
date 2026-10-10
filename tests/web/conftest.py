"""Web-test fixtures: the real app from ``create_app`` wired to a temp SQLite store,
fakes for every integration, a controllable clock and a known (cheap) Argon2
hash. Synthetic data only."""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from app.config import Settings
from app.container import build_services_from
from app.domain.models import (
    LocationPolicy,
    OutreachStatus,
    RunTrigger,
    SavedSearch,
    SearchConfig,
    SearchFilter,
    Signal,
)
from app.main import create_app
from app.persistence import SqlStore, create_all, make_engine, make_session_factory
from app.services import Services
from tests.fakes import (
    FakeCrm,
    FakeEmailFinder,
    FakeInbox,
    FakeSearchCatalog,
    FakeSignalSource,
    make_signal,
    utc,
)

NOW = utc(2026, 10, 9, 6, 0)
PASSWORD = "correct horse battery staple"  # synthetic test password
SESSION_SECRET = "test-session-secret-" + "x" * 40
# Cheap parameters keep the suite fast; verify() reads them from the hash itself.
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
PASSWORD_HASH = FAST_HASHER.hash(PASSWORD)

SEARCH = SavedSearch(
    external_id="sch_web",
    title="Web test search",
    filters=(SearchFilter(key="country", values=("United Kingdom",)),),
)

_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def make_settings(**overrides: object) -> Settings:
    """Settings that never read the developer's .env file."""
    values: dict[str, object] = {
        "app_env": "test",
        "database_url": "sqlite://",
        "admin_password_hash": PASSWORD_HASH,
        "session_secret": SESSION_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def all_routes(routes: list[Any]) -> list[Any]:
    """Flatten included routers (FastAPI wraps them; the dashboard uses no prefixes)."""
    flat: list[Any] = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            flat.extend(all_routes(list(inner.routes)))
        else:
            flat.append(route)
    return flat


def concrete_path(path: str, value: str = "1") -> str:
    """'/outreach/{outreach_id:int}/sent' -> '/outreach/1/sent'."""
    return re.sub(r"\{[^}]+\}", value, path)


def csrf_from(html: str) -> str | None:
    match = _CSRF.search(html)
    return match.group(1) if match else None


class WebClient:
    """TestClient wrapper that keeps the current CSRF token and adds it to POSTs."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.csrf = ""

    def get(self, url: str, **kwargs: Any) -> Response:
        response = self.client.get(url, **kwargs)
        token = csrf_from(response.text)
        if token:
            self.csrf = token
        return response

    def post(self, url: str, data: dict[str, Any] | None = None, **kwargs: Any) -> Response:
        payload = {"csrf_token": self.csrf, **(data or {})}
        kwargs.setdefault("follow_redirects", False)
        response = self.client.post(url, data=payload, **kwargs)
        token = csrf_from(response.text)
        if token:  # e.g. the login page re-rendered with a rotated token after a failure
            self.csrf = token
        return response

    def login(self, password: str = PASSWORD) -> Response:
        self.get("/login")
        response = self.post("/login", {"password": password})
        if response.status_code == 303:
            self.get("/")  # pick up the rotated token
        return response

    def flash(self, response: Response) -> str:
        """Follow a PRG redirect and return the page (which shows the flash), unescaped
        so assertions can use plain text. XSS tests inspect the raw HTML instead."""
        assert response.status_code == 303, response.status_code
        return html.unescape(self.get(response.headers["location"]).text)


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
    settings: Settings
    app: FastAPI

    def enable_search(self, saved: SavedSearch = SEARCH) -> SearchConfig:
        if saved not in self.catalog.searches:
            self.catalog.searches.append(saved)
        config = self.store.upsert_search_from_catalog(saved, synced_at=self.clock())
        config.enabled = True
        config.location_policy = LocationPolicy.UK_OUTSIDE_LONDON
        return self.store.save_search_config(config)

    def seed(self, *signals: Signal) -> dict[str, int]:
        """Run the real pipeline over these signals; return signal id -> outreach id."""
        self.enable_search()
        self.signals.by_filters[SEARCH.filters] = list(signals)
        self.services.origination.run(RunTrigger.CRON, use_rocketreach=False)
        return {v.founder.evertrace_signal_id: v.outreach.id or 0 for v in self.store.list_outreach()}

    def status(self, outreach_id: int) -> OutreachStatus:
        return self.store.get_outreach(outreach_id).status


@pytest.fixture(autouse=True)
def _app_loggers_enabled() -> None:
    """import-linter's ``lint_imports`` (tests/architecture) marks every logger that
    already exists as disabled. Re-enable ours so caplog assertions also hold when
    the whole suite runs in one process."""
    for name, candidate in list(logging.root.manager.loggerDict.items()):
        if name.startswith("app") and isinstance(candidate, logging.Logger):
            candidate.disabled = False


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    engine = make_engine("sqlite:///" + (tmp_path / "web.db").as_posix())
    create_all(engine)
    store = SqlStore(make_session_factory(engine))
    clock = FakeClock()
    settings = make_settings()
    catalog, signals, crm = FakeSearchCatalog(), FakeSignalSource(), FakeCrm()
    finder, inbox = FakeEmailFinder(), FakeInbox()
    services = build_services_from(
        store=store,
        catalog=catalog,
        signals=signals,
        crm=crm,
        email_finder=finder,
        inbox=inbox,
        settings=settings,
        clock=clock,
    )
    app = create_app(services=services, settings=settings)
    yield World(store, catalog, signals, crm, finder, inbox, clock, services, settings, app)
    engine.dispose()


@pytest.fixture
def anon(world: World) -> WebClient:
    return WebClient(TestClient(world.app))


@pytest.fixture
def client(world: World) -> WebClient:
    web = WebClient(TestClient(world.app))
    assert web.login().status_code == 303
    return web


@pytest.fixture
def ids(world: World) -> dict[str, int]:
    """One founder in each early status: DRAFTED, NEEDS_EMAIL, EXCLUDED (x2)."""
    return world.seed(
        make_signal(1, email="alex1@co1.example"),  # DRAFTED
        make_signal(2),  # NEEDS_EMAIL
        make_signal(3, city="London", email="alex3@co3.example"),  # EXCLUDED (location), email known
        make_signal(4, city="London"),  # EXCLUDED (location), no email
    )
