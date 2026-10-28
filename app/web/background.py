"""Automatic reply tracking while the dashboard is running.

A long-running dashboard re-checks the inbox tracker on a timer, so a founder's
reply moves them to "Replied" without anyone clicking a button. It goes through
``OriginationService.sync_tracking`` and so shares the run lock: a timer tick
during a daily run is simply skipped (the run tracks as its last step).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from starlette.concurrency import run_in_threadpool

from app.services import Services

logger = logging.getLogger(__name__)

# Let the server finish starting (and the browser open) before the first check.
FIRST_CHECK_DELAY_SECONDS = 15.0


async def tracking_loop(
    get_services: Callable[[], Services | None],
    interval_seconds: float,
    *,
    first_delay_seconds: float = FIRST_CHECK_DELAY_SECONDS,
) -> None:
    """Sync tracking every ``interval_seconds`` until cancelled. Never raises:
    one failed sync is logged and the next tick tries again."""
    await asyncio.sleep(first_delay_seconds)
    while True:
        services = get_services()
        if services is not None:
            await _sync_once(services)
        await asyncio.sleep(interval_seconds)


async def _sync_once(services: Services) -> None:
    try:
        result = await run_in_threadpool(services.origination.sync_tracking)
    except Exception as exc:
        logger.error("Automatic tracking sync failed (%s)", type(exc).__name__)
        return
    if result is None:
        logger.info("Automatic tracking sync skipped: a run is in progress")
    elif result.updates or result.errors:
        logger.info(
            "Automatic tracking sync: %d checked, %d update(s), %d error(s)",
            result.checked,
            result.updates,
            len(result.errors),
        )
