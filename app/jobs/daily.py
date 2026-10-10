"""Daily origination run: ``python -m app.jobs.daily`` (the Railway cron service).

Exit codes: 0 for SUCCEEDED / PARTIAL / SKIPPED (the run did what it could and
recorded its errors), 1 for FAILED, 2 for bad arguments (e.g. unknown search).
A JSON summary of the run is printed to stdout.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from app.domain.models import RunRecord, RunState, RunTrigger
from app.jobs import _runtime
from app.services import ServiceError, Services

logger = logging.getLogger(__name__)

OK_STATES = frozenset({RunState.SUCCEEDED, RunState.PARTIAL, RunState.SKIPPED})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.daily", description="Run the daily origination pipeline once."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not advance watermarks, call RocketReach or sync tracking.",
    )
    parser.add_argument("--no-rocketreach", action="store_true", help="Skip RocketReach email lookups.")
    parser.add_argument(
        "--search",
        metavar="EXTERNAL_ID",
        help="Run only this Evertrace saved search (e.g. sch_...), even if it is disabled.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Record the run as manually triggered rather than by cron.",
    )
    return parser


def summarise(run: RunRecord) -> dict[str, object]:
    return {
        "run_id": run.id,
        "state": run.state.value,
        "trigger": run.trigger.value,
        "dry_run": run.dry_run,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "counts": run.counts.model_dump(),
        "errors": list(run.errors),
    }


def _resolve_search(services: Services, external_id: str) -> int | None:
    """Local id for an Evertrace search id, syncing the catalogue first if needed."""
    found = services.searches.find_by_external_id(external_id)
    if found is None:
        try:
            services.searches.sync_catalog()
        except ServiceError as exc:
            logger.warning("Search catalogue sync failed: %s", exc.message)
        found = services.searches.find_by_external_id(external_id)
    return found.id if found is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _settings, services = _runtime.start()
    try:
        only_search_id: int | None = None
        if args.search:
            only_search_id = _resolve_search(services, args.search.strip())
            if only_search_id is None:
                _runtime.emit({"state": "error", "errors": ["Unknown search id"]})
                return 2
        run = services.origination.run(
            RunTrigger.MANUAL if args.manual else RunTrigger.CRON,
            dry_run=args.dry_run,
            use_rocketreach=not args.no_rocketreach,
            only_search_id=only_search_id,
        )
        _runtime.emit(summarise(run))
        return 0 if run.state in OK_STATES else 1
    finally:
        services.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
