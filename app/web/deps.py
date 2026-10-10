"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.domain.models import RunRecord
from app.services import Services

# How the Runs page reads history: (services, limit) -> newest-first runs.
RunHistory = Callable[[Services, int], list[RunRecord]]
Clock = Callable[[], datetime]

MAX_ID = 2**31 - 1


def get_services(request: Request) -> Services:
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:
        raise HTTPException(status_code=503, detail="The dashboard is still starting up")
    return services


def get_run_history(request: Request) -> RunHistory:
    history: RunHistory = request.app.state.run_history
    return history


def latest_run_only(services: Services, limit: int) -> list[RunRecord]:
    """Fallback history when the composition root offers nothing better."""
    latest = services.outreach.dashboard_summary().latest_run
    return [latest] if latest is not None else []


ServicesDep = Annotated[Services, Depends(get_services)]
RunHistoryDep = Annotated[RunHistory, Depends(get_run_history)]
