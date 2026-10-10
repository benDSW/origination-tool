"""Shared fixtures. One httpx.Client for the whole session: building one loads the
CA bundle, which is slow on some machines, and respx intercepts it either way."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest


@pytest.fixture(scope="session")
def http_client() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client
