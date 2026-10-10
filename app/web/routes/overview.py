"""Overview, Run now, Sync tracking and run history."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Request
from starlette.responses import Response

from app.domain.models import OutreachStatus, RunTrigger
from app.services import Services
from app.web.deps import RunHistoryDep, ServicesDep
from app.web.rendering import redirect, render

logger = logging.getLogger(__name__)
router = APIRouter()

RUN_HISTORY_LIMIT = 50


def run_in_background(services: Services, only_search_id: int | None = None) -> None:
    """Background task body. The service's run lock stops a double click (or the
    cron job) from starting a second concurrent run."""
    try:
        run = services.origination.run(RunTrigger.MANUAL, only_search_id=only_search_id)
        logger.info("Manual run %s finished: %s", run.id, run.state.value)
    except Exception as exc:
        logger.error("Manual run failed (%s)", type(exc).__name__)


@router.get("/")
def overview(request: Request, services: ServicesDep) -> Response:
    summary = services.outreach.dashboard_summary()
    return render(request, "overview.html", {"summary": summary, "statuses": list(OutreachStatus)})


@router.post("/runs")
def start_run(request: Request, services: ServicesDep, background: BackgroundTasks) -> Response:
    background.add_task(run_in_background, services)
    return redirect(
        request,
        "/",
        "Run started in the background. Refresh in a minute to see the result "
        "(if a run was already in progress, this one is skipped).",
        "success",
    )


@router.post("/tracking/sync")
def sync_tracking(request: Request, services: ServicesDep) -> Response:
    result = services.origination.sync_tracking()
    if result is None:
        return redirect(request, "/", "A run is in progress; tracking runs as part of it.", "info")
    message = f"Tracking checked {result.checked} founder(s): {result.updates} status update(s)."
    if result.errors:
        first = result.errors[0][:200]
        return redirect(request, "/", f"{message} {len(result.errors)} error(s), e.g. {first}", "error")
    return redirect(request, "/", message, "success")


@router.get("/runs")
def runs(request: Request, services: ServicesDep, history: RunHistoryDep) -> Response:
    return render(request, "runs.html", {"runs": history(services, RUN_HISTORY_LIMIT)})
