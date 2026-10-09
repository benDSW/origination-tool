"""Safe email-template rendering and email-address validation.

Templates are edited by the user and filled with *untrusted* Evertrace data, so
rendering is a plain whitelist substitution of ``{field}`` placeholders:
no Jinja, no ``str.format`` (which allows attribute access), no expressions.
"""

from __future__ import annotations

import re

from email_validator import EmailNotValidError, validate_email

from app.domain.models import Draft, EmailTemplate, FounderRecord

ALLOWED_FIELDS: frozenset[str] = frozenset({"first_name", "last_name", "full_name", "company_name"})

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINE_BREAKS = re.compile(r"[\r\n]+")

DEFAULT_SUBJECT = "Funding Enquiry"
DEFAULT_BODY = (
    "Hi {first_name},\n"
    "\n"
    "My name is Ben Fracz and I am an investor at DSW Ventures – a VC based in Manchester that "
    "invests across the UK. We invest up to £2m in SEIS or EIS-qualifying companies and are "
    "looking to deploy £10m in equity funding over the next 12 months in early-stage businesses.\n"
    "\n"
    "We recently came across your company and would like to learn more about your current projects "
    "and explore potential investment opportunities. If you're planning any fundraising rounds, we'd "
    "be keen to discuss how we might support your vision.\n"
    "\n"
    "Kind regards,\n"
    "Ben"
)
DEFAULT_TEMPLATE = EmailTemplate(subject=DEFAULT_SUBJECT, body=DEFAULT_BODY, version=1)

MAX_SUBJECT_LEN = 200
MAX_BODY_LEN = 5000


class TemplateError(ValueError):
    pass


def placeholders(text: str) -> set[str]:
    return {m.group(1).strip() for m in _PLACEHOLDER.finditer(text)}


def validate_template(subject: str, body: str) -> list[str]:
    """Return a list of problems (empty == valid)."""
    problems: list[str] = []
    if not subject.strip():
        problems.append("Subject is empty")
    if not body.strip():
        problems.append("Body is empty")
    if len(subject) > MAX_SUBJECT_LEN:
        problems.append(f"Subject longer than {MAX_SUBJECT_LEN} characters")
    if len(body) > MAX_BODY_LEN:
        problems.append(f"Body longer than {MAX_BODY_LEN} characters")
    if _LINE_BREAKS.search(subject):
        problems.append("Subject must be a single line")
    unknown = (placeholders(subject) | placeholders(body)) - ALLOWED_FIELDS
    for name in sorted(unknown):
        allowed = ", ".join("{" + f + "}" for f in sorted(ALLOWED_FIELDS))
        problems.append(f"Unknown merge field {{{name}}} (allowed: {allowed})")
    return problems


def _clean_value(value: str | None) -> str:
    if not value:
        return ""
    return _CONTROL.sub("", _LINE_BREAKS.sub(" ", value)).strip()


def merge_values(founder: FounderRecord) -> dict[str, str]:
    return {
        "first_name": _clean_value(founder.first_name) or "there",
        "last_name": _clean_value(founder.last_name),
        "full_name": _clean_value(founder.full_name),
        "company_name": _clean_value(founder.company_name) or "your company",
    }


def render(text: str, values: dict[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key not in ALLOWED_FIELDS:
            raise TemplateError(f"Unknown merge field {{{key}}}")
        return values.get(key, "")

    return _PLACEHOLDER.sub(_sub, text)


def normalise_email(value: str | None) -> str | None:
    """Return a normalised, syntactically valid single address, or ``None``.

    No DNS lookups (deliverability is RocketReach's job). Rejects anything with
    line breaks, commas or multiple addresses (header-injection defence).
    """
    if not value:
        return None
    candidate = value.strip()
    if any(ch in candidate for ch in "\r\n,;<>\"' ") or len(candidate) > 254:
        return None
    try:
        result = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        return None
    return result.normalized


def build_draft(template: EmailTemplate, founder: FounderRecord, email: str) -> Draft:
    to = normalise_email(email)
    if to is None:
        raise TemplateError("Recipient email is not valid")
    values = merge_values(founder)
    subject = _LINE_BREAKS.sub(" ", render(template.subject, values)).strip()
    body = render(template.body, values).replace("\r\n", "\n")
    return Draft(to=to, subject=subject, body=body)
