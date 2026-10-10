"""R20: the layer boundaries in .importlinter must hold."""

import logging
import os
from pathlib import Path

import pytest
from importlinter.cli import lint_imports

from app.domain import ports
from tests import fakes

ROOT = Path(__file__).resolve().parents[2]


def _logger_state() -> dict[str, tuple[bool, bool, int]]:
    return {
        name: (logger.disabled, logger.propagate, logger.level)
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }


def test_import_contracts_hold(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    # lint_imports runs logging.config.dictConfig with disable_existing_loggers=True,
    # which silently disables every app logger created so far. Restore the state so
    # later tests that assert on log output (caplog) are not polluted.
    saved = _logger_state()
    try:
        exit_code = lint_imports(
            config_filename=os.path.join(ROOT, ".importlinter"), no_cache=True, no_logo=True
        )
    finally:
        for name, (disabled, propagate, level) in saved.items():
            logger = logging.getLogger(name)
            logger.disabled, logger.propagate = disabled, propagate
            logger.setLevel(level)
    assert exit_code == 0, capsys.readouterr().out


def test_fakes_satisfy_ports() -> None:
    """R21: every integration port has a fake implementation."""
    assert isinstance(fakes.FakeSearchCatalog(), ports.SearchCatalog)
    assert isinstance(fakes.FakeSignalSource(), ports.SignalSource)
    assert isinstance(fakes.FakeEmailFinder(), ports.EmailFinder)
    assert isinstance(fakes.FakeCrm(), ports.CrmGateway)
    assert isinstance(fakes.FakeInbox(), ports.InboxTracker)
