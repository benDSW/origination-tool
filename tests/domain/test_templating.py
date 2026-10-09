"""R12 + R23: safe template rendering, header-injection and email validation."""

import pytest

from app.domain.models import Draft, EmailTemplate, FounderRecord
from app.domain.templating import (
    DEFAULT_TEMPLATE,
    TemplateError,
    build_draft,
    merge_values,
    normalise_email,
    render,
    validate_template,
)
from tests.fakes import utc


def founder(**kw: object) -> FounderRecord:
    base: dict[str, object] = {
        "id": 1,
        "evertrace_signal_id": "sig_1",
        "first_name": "Jo",
        "last_name": "Bloggs",
        "company_name": "Acme Ltd",
        "discovered_at": utc(2026, 10, 1),
        "created_at": utc(2026, 10, 1),
    }
    base.update(kw)
    return FounderRecord(**base)  # type: ignore[arg-type]


def test_default_template_is_valid_and_matches_spec() -> None:
    assert validate_template(DEFAULT_TEMPLATE.subject, DEFAULT_TEMPLATE.body) == []
    assert DEFAULT_TEMPLATE.subject == "Funding Enquiry"
    assert DEFAULT_TEMPLATE.body.startswith("Hi {first_name},")


def test_build_draft_renders_merge_fields() -> None:
    d = build_draft(
        EmailTemplate(subject="Hi {company_name}", body="Dear {first_name} {last_name}"),
        founder(),
        "jo@acme.co",
    )
    assert d == Draft(to="jo@acme.co", subject="Hi Acme Ltd", body="Dear Jo Bloggs")
    assert d.as_text() == "To: jo@acme.co\nSubject: Hi Acme Ltd\n\nDear Jo Bloggs"


def test_missing_values_fall_back() -> None:
    values = merge_values(founder(first_name="", company_name=None))
    assert values["first_name"] == "there"
    assert values["company_name"] == "your company"


@pytest.mark.parametrize(
    "evil",
    ["{first_name.__class__}", "{0}", "{__import__}", "{ first_name.upper }"],
)
def test_unknown_or_expression_placeholders_are_rejected(evil: str) -> None:
    assert validate_template("s", f"Hi {evil}")
    with pytest.raises(TemplateError):
        render(f"Hi {evil}", merge_values(founder()))


def test_untrusted_values_are_inserted_literally() -> None:
    f = founder(first_name="{last_name}<script>alert(1)</script>")
    body = render("Hi {first_name}", merge_values(f))
    # No recursive expansion; HTML escaping is the template engine's job at display time.
    assert body == "Hi {last_name}<script>alert(1)</script>"


def test_header_injection_via_name_is_neutralised_in_subject() -> None:
    f = founder(first_name="Jo\r\nBcc: victim@example.com")
    d = build_draft(EmailTemplate(subject="Hello {first_name}", body="b"), f, "jo@acme.co")
    assert "\n" not in d.subject and "\r" not in d.subject


def test_template_validation_rules() -> None:
    assert "Subject is empty" in validate_template(" ", "b")
    assert "Body is empty" in validate_template("s", "")
    assert "Subject must be a single line" in validate_template("a\nb", "b")
    assert any("longer" in p for p in validate_template("s" * 201, "b"))
    assert any("longer" in p for p in validate_template("s", "b" * 5001))


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("jo@acme.co", True),
        ("  Jo@Acme.co ", True),
        ("not-an-email", False),
        ("a@b.co\r\nBcc: x@y.z", False),
        ("a@b.co, c@d.co", False),
        ("Jo <jo@acme.co>", False),
        ("", False),
        (None, False),
    ],
)
def test_normalise_email(raw: str | None, ok: bool) -> None:
    assert (normalise_email(raw) is not None) is ok


def test_build_draft_rejects_invalid_recipient() -> None:
    with pytest.raises(TemplateError):
        build_draft(DEFAULT_TEMPLATE, founder(), "bad\naddress")
