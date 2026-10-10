"""Email template editor (R12, R14) with a JSON live-preview endpoint."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from starlette.responses import JSONResponse, Response

from app.domain.templating import ALLOWED_FIELDS, MAX_BODY_LEN, MAX_SUBJECT_LEN
from app.services import Services, TemplateValidationError
from app.web.deps import ServicesDep
from app.web.rendering import redirect, render

router = APIRouter()

# Generous transport caps; the service applies the real limits with friendly messages.
SubjectField = Annotated[str, Form(max_length=MAX_SUBJECT_LEN * 5)]
BodyField = Annotated[str, Form(max_length=MAX_BODY_LEN * 4)]


def _context(services: Services, **overrides: Any) -> dict[str, Any]:
    current = services.templates.current()
    context: dict[str, Any] = {
        "template": current,
        "subject": current.subject,
        "body": current.body,
        "fields": sorted(ALLOWED_FIELDS),
        "problems": [],
        "max_subject": MAX_SUBJECT_LEN,
        "max_body": MAX_BODY_LEN,
    }
    context.update(overrides)
    return context


@router.get("/template")
def template_page(request: Request, services: ServicesDep) -> Response:
    return render(request, "template.html", _context(services))


@router.post("/template")
def save_template(
    request: Request,
    services: ServicesDep,
    subject: SubjectField = "",
    body: BodyField = "",
    rerender: Annotated[bool, Form()] = False,
) -> Response:
    try:
        if rerender:
            template, changed = services.save_template(subject, body)
        else:
            template, changed = services.templates.save(subject, body), None
    except TemplateValidationError as exc:
        context = _context(services, subject=subject, body=body, problems=exc.problems)
        return render(request, "template.html", context, status_code=400)
    if changed is None:
        message = (
            f"Template saved as version {template.version}. Open drafts keep their old wording "
            "until you re-render them."
        )
    else:
        message = f"Template saved as version {template.version}; {changed} open draft(s) re-rendered."
    return redirect(request, "/template", message, "success")


@router.post("/template/rerender")
def rerender(request: Request, services: ServicesDep) -> Response:
    changed = services.outreach.rerender_open_drafts()
    return redirect(
        request, "/template", f"{changed} open draft(s) re-rendered with the current template.", "success"
    )


@router.post("/template/preview")
def preview(services: ServicesDep, subject: SubjectField = "", body: BodyField = "") -> Response:
    """Live preview for app.js (the CSRF token arrives in the X-CSRF-Token header)."""
    try:
        draft = services.templates.preview(subject, body)
    except TemplateValidationError as exc:
        return JSONResponse({"ok": False, "problems": exc.problems}, status_code=422)
    return JSONResponse(
        {"ok": True, "to": draft.to, "subject": draft.subject, "body": draft.body, "text": draft.as_text()}
    )
