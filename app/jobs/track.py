"""Tracking only: ``python -m app.jobs.track``.

Checks every DRAFTED / SENT outreach for sent / replied emails, under the same
lock as the daily run. Exit 0 when the sync ran cleanly or another run was in
progress, 1 if any founder errored.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from app.jobs import _runtime


def main(argv: Sequence[str] | None = None) -> int:
    argparse.ArgumentParser(
        prog="python -m app.jobs.track", description="Sync sent / replied status from the inbox tracker."
    ).parse_args(argv)
    _settings, services = _runtime.start()
    try:
        result = services.origination.sync_tracking()
        if result is None:
            _runtime.emit({"state": "skipped", "reason": "Another run was in progress"})
            return 0
        _runtime.emit(
            {
                "state": "partial" if result.errors else "succeeded",
                "checked": result.checked,
                "updates": result.updates,
                "errors": result.errors,
            }
        )
        return 1 if result.errors else 0
    finally:
        services.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
