"""R24: the Railway web and cron services are defined as code and documented, and
migrations run on every web deploy before the app starts serving."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.jobs import daily
from app.main import create_app
from tests.web.conftest import make_settings

ROOT = Path(__file__).resolve().parents[2]
SECRET_KEYS = (
    "EVERTRACE_API_KEY",
    "ROCKETREACH_API_KEY",
    "AFFINITY_API_KEY",
    "ADMIN_PASSWORD_HASH",
    "SESSION_SECRET",
    "SUPPRESSION_SECRET",
)


def _deploy(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))["deploy"]


def test_web_service_migrates_then_serves_with_a_health_check() -> None:
    deploy = _deploy("railway.json")
    first, _, second = deploy["startCommand"].partition(" && ")
    # `&&`: if a migration fails the new version never starts serving.
    assert shlex.split(first) == ["python", "-m", "alembic", "upgrade", "head"]
    serve = shlex.split(second)
    assert serve[:4] == ["python", "-m", "uvicorn", "app.main:app"]
    assert "--proxy-headers" in serve  # Railway terminates TLS; needed for Secure cookies / HSTS
    assert serve[serve.index("--port") + 1] == "$PORT"
    assert deploy["healthcheckPath"] == "/healthz"
    # Railway probes before services exist and without a session: it must still answer 200.
    app = create_app(settings=make_settings())
    response = TestClient(app).get(deploy["healthcheckPath"])
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_cron_service_runs_the_daily_job_at_06_utc_without_restarts() -> None:
    deploy = _deploy("railway.cron.json")
    assert shlex.split(deploy["startCommand"]) == ["python", "-m", "app.jobs.daily"]
    assert deploy["cronSchedule"] == "0 6 * * *"
    assert deploy["restartPolicyType"] == "NEVER"  # a failed run must not loop and burn credits
    assert callable(daily.main)


def test_deployment_doc_covers_both_services_and_migrations() -> None:
    doc = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for needle in (
        "railway.json",
        "railway.cron.json",
        "python -m app.jobs.daily",
        "alembic upgrade head",
        "0 6 * * *",
        "/healthz",
    ):
        assert needle in doc, needle


def test_env_example_lists_settings_but_holds_no_secret_values() -> None:
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    for key in SECRET_KEYS:
        assert key in values, key
        assert values[key] == "", f"{key} must be blank in .env.example"
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()
