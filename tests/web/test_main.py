"""Composition entry point (app.main): lazy app, lifespan-built services, config checks."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main
from app.domain.models import RunRecord, RunTrigger
from app.web import InsecureConfigError, WebConfig, latest_run_only, resolve_session_secret
from tests.web.conftest import NOW, SESSION_SECRET, WebClient, World, make_settings


def test_importing_app_main_builds_nothing() -> None:
    # The app is created lazily on first attribute access (what uvicorn does), so a
    # plain import neither reads the environment nor defines a module-level app.
    assert "app" not in vars(main)
    with pytest.raises(AttributeError):
        main.not_a_thing  # noqa: B018


def test_lazy_app_uses_settings_and_redacting_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(main, "_app", None)
    monkeypatch.setattr(main, "get_settings", lambda: make_settings())
    monkeypatch.setattr(main, "configure_logging", lambda secrets: calls.append(list(secrets)))
    first = main.app
    assert isinstance(first, FastAPI)
    assert main.app is first  # cached
    assert calls and SESSION_SECRET in calls[0]
    monkeypatch.setattr(main, "_app", None)


def test_lifespan_builds_services_at_startup_and_closes_them(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    world.services.closers.append(lambda: closed.append(True))
    built: list[object] = []

    def fake_build(settings: object) -> object:
        built.append(settings)
        return world.services

    monkeypatch.setattr(main, "build_services", fake_build)
    app = main.create_app(settings=make_settings())
    assert app.state.services is None  # nothing built at creation time
    with TestClient(app) as raw:
        assert app.state.services is world.services and len(built) == 1
        web = WebClient(raw)
        assert web.login().status_code == 303
        assert web.get("/drafts").status_code == 200
    assert closed == [True] and app.state.services is None


def test_healthz_needs_no_services_and_no_login() -> None:
    app = main.create_app(settings=make_settings())  # lifespan not started: no services
    raw = TestClient(app)
    response = raw.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    web = WebClient(raw)
    # No services yet means no server-side session store: nobody can log in (fail closed).
    assert web.login().status_code == 503
    response = raw.get("/drafts", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_production_refuses_a_weak_session_secret() -> None:
    for secret in ("", "short-secret"):
        with pytest.raises(InsecureConfigError, match="SESSION_SECRET"):
            main.create_app(settings=make_settings(app_env="production", session_secret=secret))


def test_insecure_config_error_never_contains_the_secret() -> None:
    with pytest.raises(InsecureConfigError) as info:
        resolve_session_secret("weak-secret-value", production=True)
    assert "weak-secret-value" not in str(info.value)


def test_development_uses_an_ephemeral_secret_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        first = resolve_session_secret("", production=False)
        second = resolve_session_secret("short", production=False)
    assert len(first) >= 32 and first != second
    assert "ephemeral random secret" in caplog.text
    assert resolve_session_secret(SESSION_SECRET, production=True) == SESSION_SECRET


def test_web_config_repr_hides_the_secret() -> None:
    config = WebConfig(session_secret=SESSION_SECRET)
    assert SESSION_SECRET not in repr(config)
    assert not config.https_only and WebConfig(session_secret=SESSION_SECRET, production=True).https_only


def _run(n: int) -> RunRecord:
    return RunRecord(id=n, trigger=RunTrigger.CRON, started_at=NOW)


def test_run_history_prefers_a_public_list_runs() -> None:
    services = SimpleNamespace(
        origination=SimpleNamespace(list_runs=lambda limit: [_run(1), _run(2)][:limit])
    )
    assert [r.id for r in main.run_history(services, 1)] == [1]  # type: ignore[arg-type]


def test_run_history_falls_back_to_latest_run() -> None:
    summary = SimpleNamespace(latest_run=_run(7))
    services = SimpleNamespace(
        origination=SimpleNamespace(), outreach=SimpleNamespace(dashboard_summary=lambda: summary)
    )
    assert [r.id for r in main.run_history(services, 5)] == [7]  # type: ignore[arg-type]
    summary.latest_run = None
    assert latest_run_only(services, 5) == []  # type: ignore[arg-type]


def test_run_history_reads_the_store(world: World) -> None:
    world.services.origination.run(RunTrigger.CRON)
    world.services.origination.run(RunTrigger.MANUAL)
    runs = main.run_history(world.services, 50)
    assert [r.trigger for r in runs] == [RunTrigger.MANUAL, RunTrigger.CRON]
