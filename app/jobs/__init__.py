"""Command-line jobs (cron entrypoints and admin tools).

Each module is runnable as ``python -m app.jobs.<name>`` and exposes
``main(argv=None) -> int`` so tests can call it directly.
"""
