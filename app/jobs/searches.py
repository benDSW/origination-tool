"""Manage saved searches from the terminal: ``python -m app.jobs.searches <command>``.

Commands:
  list                         show every synced search and its local settings
  sync                         pull the saved-search list from Evertrace
  enable ID | disable ID       turn a search on / off for the daily run
  policy ID uk_outside_london|london_allowed
  lookback ID DAYS             days to backfill on a search's first run (1-730)

ID is the local number shown by ``list`` or the Evertrace id (``sch_...``).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from app.domain.models import LocationPolicy, SearchConfig
from app.jobs import _runtime
from app.services import ServiceError, Services

_EPILOG = """commands:
  list                         show every synced search and its local settings
  sync                         pull the saved-search list from Evertrace
  enable ID | disable ID       turn a search on / off for the daily run
  policy ID POLICY             uk_outside_london | london_allowed
  lookback ID DAYS             days to backfill on a search's first run (1-730)

ID is the local number shown by 'list' or the Evertrace id (sch_...)."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.searches",
        description="Manage Evertrace saved searches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Show searches")
    sub.add_parser("sync", help="Sync searches from Evertrace")
    for name in ("enable", "disable"):
        p = sub.add_parser(name, help=f"{name.capitalize()} a search")
        p.add_argument("id")
    p = sub.add_parser("policy", help="Set a search's location policy")
    p.add_argument("id")
    p.add_argument("policy", choices=[x.value for x in LocationPolicy])
    p = sub.add_parser("lookback", help="Set a search's lookback window in days")
    p.add_argument("id")
    p.add_argument("days", type=int)
    return parser


def format_table(configs: Sequence[SearchConfig]) -> str:
    if not configs:
        return "No searches yet. Run: python -m app.jobs.searches sync"
    lines = [f"{'ID':>4}  {'ON':<3} {'POLICY':<18} {'DAYS':>4}  {'WATERMARK':<20} {'EVERTRACE ID':<24} TITLE"]
    for c in configs:
        mark = c.watermark.strftime("%Y-%m-%d %H:%M UTC") if c.watermark else "-"
        lines.append(
            f"{c.id or 0:>4}  {'yes' if c.enabled else 'no':<3} {c.location_policy.value:<18} "
            f"{c.lookback_days:>4}  {mark:<20} {c.external_id:<24} {c.title}"
        )
    return "\n".join(lines)


def _resolve(services: Services, ident: str) -> int:
    ident = ident.strip()
    if ident.isdigit():
        return int(ident)
    found = services.searches.find_by_external_id(ident)
    if found is None or found.id is None:
        raise LookupError(ident)
    return found.id


def _run(services: Services, args: argparse.Namespace) -> list[SearchConfig] | SearchConfig:
    command = args.command
    if command == "list":
        return services.searches.list_configs()
    if command == "sync":
        return services.searches.sync_catalog()
    search_id = _resolve(services, args.id)
    if command in ("enable", "disable"):
        return services.searches.update_config(search_id, enabled=command == "enable")
    if command == "policy":
        return services.searches.update_config(search_id, location_policy=args.policy)
    return services.searches.update_config(search_id, lookback_days=args.days)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _settings, services = _runtime.start()
    try:
        try:
            result = _run(services, args)
        except LookupError:
            sys.stderr.write("No search with that id. Run 'list' to see ids.\n")
            return 2
        except ServiceError as exc:
            sys.stderr.write(f"Error: {exc.message}\n")
            return 1
        configs = result if isinstance(result, list) else [result]
        sys.stdout.write(format_table(configs) + "\n")
        return 0
    finally:
        services.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
