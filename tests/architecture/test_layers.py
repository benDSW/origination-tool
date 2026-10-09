"""R20: the layer boundaries in .importlinter must hold."""

import os
from pathlib import Path

import pytest
from importlinter.cli import lint_imports

from app.domain import ports
from tests import fakes

ROOT = Path(__file__).resolve().parents[2]


def test_import_contracts_hold(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    exit_code = lint_imports(config_filename=os.path.join(ROOT, ".importlinter"), no_cache=True, no_logo=True)
    assert exit_code == 0, capsys.readouterr().out


def test_fakes_satisfy_ports() -> None:
    """R21: every integration port has a fake implementation."""
    assert isinstance(fakes.FakeSearchCatalog(), ports.SearchCatalog)
    assert isinstance(fakes.FakeSignalSource(), ports.SignalSource)
    assert isinstance(fakes.FakeEmailFinder(), ports.EmailFinder)
    assert isinstance(fakes.FakeCrm(), ports.CrmGateway)
    assert isinstance(fakes.FakeInbox(), ports.InboxTracker)
