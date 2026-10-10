"""Shared fixtures for RocketReach tests. Synthetic data only - no real people."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

from app.integrations.rocketreach.client import BASE_URL, RocketReachClient

API_KEY = "rr_SYNTHETIC_test_key_0123456789"
SLUG = "alex-testfounder-1"
PROFILE_URL = f"https://www.linkedin.com/in/{SLUG}"


@dataclass
class FakeTime:
    """Injected clock + sleep: sleeping advances the clock, nothing actually waits."""

    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture(scope="session")
def shared_http() -> Iterator[httpx.Client]:
    """One real httpx.Client for the session: building its TLS context is slow on Windows.

    respx patches the transport layer, so requests through it are still mocked.
    """
    with httpx.Client(follow_redirects=False) as http:
        yield http


@pytest.fixture
def client(fake_time: FakeTime, shared_http: httpx.Client) -> Iterator[RocketReachClient]:
    c = RocketReachClient(API_KEY, http=shared_http, sleep=fake_time.sleep, clock=fake_time.clock)
    yield c
    c.close()


def profile(**overrides: Any) -> dict[str, Any]:
    """A synthetic, complete RocketReach profile."""
    base: dict[str, Any] = {
        "id": 4242,
        "status": "complete",
        "name": "Alex Testfounder",
        "linkedin_url": PROFILE_URL,
        "current_employer": "Example1 Limited",
        "current_title": "Founder",
        "emails": [
            {"email": "alex@co1.example", "smtp_valid": "valid", "type": "professional", "grade": "A"},
            {"email": "alex.t@mail.example", "smtp_valid": "valid", "type": "personal", "grade": "A"},
        ],
    }
    base.update(overrides)
    return base
