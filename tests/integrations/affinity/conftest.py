"""Shared fixtures. Every test talks to a respx router that only mocks GET routes,
and teardown proves no other HTTP method was ever sent (Affinity is read-only)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import httpx
import pytest
import respx

from app.integrations.affinity.adapter import AffinityAdapter
from app.integrations.affinity.client import AffinityClient
from tests.fakes import utc

FAKE_KEY = "synthetic-affinity-key-0000"
NOW = utc(2026, 10, 9, 12, 0)
DSW_DOMAINS = ("dsw.example",)
ORIGINATION_LIST = 1001
APPLICANT_LIST = 2002

WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE"]


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        writes = mock.route(method__in=WRITE_METHODS).mock(return_value=httpx.Response(405))
        yield mock
        assert not writes.called, "Affinity integration must never send a write request"
        assert all(call.request.method == "GET" for call in mock.calls)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture(scope="session")
def shared_http() -> Iterator[httpx.Client]:
    """One transport for the whole session: building an SSL context per test is slow on Windows.
    respx patches below the client, so requests are still intercepted by each test's router."""
    with httpx.Client(follow_redirects=False) as http:
        yield http


@pytest.fixture
def client(router: respx.MockRouter, sleeps: list[float], shared_http: httpx.Client) -> AffinityClient:
    return AffinityClient(FAKE_KEY, http=shared_http, sleep=sleeps.append)


def fixed_clock() -> datetime:
    return NOW


@pytest.fixture
def adapter(client: AffinityClient) -> AffinityAdapter:
    return AffinityAdapter(
        client,
        list_ids=[ORIGINATION_LIST, APPLICANT_LIST],
        dsw_email_domains=DSW_DOMAINS,
        clock=fixed_clock,
    )
