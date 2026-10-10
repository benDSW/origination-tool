"""TemplateService (R12, R14): validation, versioning, preview with synthetic data."""

from __future__ import annotations

import pytest

from app.domain.templating import DEFAULT_BODY, DEFAULT_SUBJECT
from app.services import TemplateValidationError, ValidationFailed
from app.services.templates import SAMPLE_EMAIL, TemplateService
from tests.services.conftest import World


def _svc(world: World) -> TemplateService:
    return world.services.templates


def test_current_is_the_seeded_default(world: World) -> None:
    template = _svc(world).current()
    assert (template.subject, template.body, template.version) == (DEFAULT_SUBJECT, DEFAULT_BODY, 1)


def test_save_creates_a_new_version_and_normalises_line_endings(world: World) -> None:
    saved = _svc(world).save("  Hello {first_name}  ", "Line one\r\nLine two\rLine three")
    assert saved.version == 2
    assert saved.subject == "Hello {first_name}"
    assert saved.body == "Line one\nLine two\nLine three"
    assert _svc(world).current() == saved


@pytest.mark.parametrize(
    ("subject", "body", "problem"),
    [
        ("Hi {nickname}", "Body", "Unknown merge field {nickname}"),
        ("", "Body", "Subject is empty"),
        ("Subject", "  ", "Body is empty"),
        ("Hi", "{founder.__class__}", "Unknown merge field"),
    ],
)
def test_invalid_templates_are_rejected(world: World, subject: str, body: str, problem: str) -> None:
    with pytest.raises(TemplateValidationError) as info:
        _svc(world).save(subject, body)
    assert any(problem in p for p in info.value.problems)
    assert isinstance(info.value, ValidationFailed)
    assert _svc(world).current().version == 1


def test_preview_uses_a_synthetic_founder(world: World) -> None:
    draft = _svc(world).preview("Hello {first_name}", "Dear {full_name} of {company_name}")
    assert draft.to == SAMPLE_EMAIL
    assert draft.subject == "Hello Sam"
    assert draft.body == "Dear Sam Sample of Sample Robotics"
    assert _svc(world).current().version == 1  # preview never saves


def test_preview_rejects_invalid_templates(world: World) -> None:
    with pytest.raises(TemplateValidationError):
        _svc(world).preview("Hi {unknown}", "Body")


def test_validation_error_message_joins_problems() -> None:
    err = TemplateValidationError(["a", "b"])
    assert err.message == "a; b"
    assert ValidationFailed([]).problems == ["Invalid input"]
