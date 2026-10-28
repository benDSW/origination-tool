"""Drafts, Needs email, Excluded, Pipeline and the outreach detail page, plus every
button that moves an outreach record (all POST, all through OutreachService)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Form, Path, Request
from starlette.responses import Response

from app.domain.models import CloseReason, OutreachStatus
from app.services import ServiceError
from app.web.auth import MAX_NEXT_LEN, safe_next
from app.web.deps import MAX_ID, ServicesDep
from app.web.rendering import redirect, render

router = APIRouter()
S = OutreachStatus

OutreachId = Annotated[int, Path(ge=1, le=MAX_ID)]
NextField = Annotated[str, Form(alias="next", max_length=MAX_NEXT_LEN)]
CLOSE_REASONS: tuple[CloseReason, ...] = (
    CloseReason.DECLINED,
    CloseReason.LAPSED,
    CloseReason.NOT_RAISING,
    CloseReason.SKIPPED,
)
LIST_LIMIT = 500


def _act(request: Request, action: Callable[[], object], *, success: str, next_url: str) -> Response:
    """Run one service action and redirect back (PRG) with a flash message."""
    target = safe_next(next_url)
    try:
        action()
    except ServiceError as exc:
        return redirect(request, target, exc.message, "error")
    return redirect(request, target, success, "success")


# --------------------------------------------------------------------------- #
# Pages (GET never changes anything)
# --------------------------------------------------------------------------- #


@router.get("/drafts")
def drafts(request: Request, services: ServicesDep) -> Response:
    items = services.outreach.list_by_status([S.DRAFTED], limit=LIST_LIMIT)
    return render(request, "drafts.html", {"items": items})


@router.get("/needs-email")
def needs_email(request: Request, services: ServicesDep) -> Response:
    items = services.outreach.list_by_status([S.NEEDS_EMAIL], limit=LIST_LIMIT)
    return render(request, "needs_email.html", {"items": items})


@router.get("/excluded")
def excluded(request: Request, services: ServicesDep) -> Response:
    items = services.outreach.list_by_status([S.EXCLUDED], limit=LIST_LIMIT)
    return render(request, "excluded.html", {"items": items})


@router.get("/pipeline")
def pipeline(request: Request, services: ServicesDep) -> Response:
    board = services.outreach.pipeline()
    columns = [(status, board.columns.get(status, [])) for status in (S.REPLIED, S.SENT, S.DECK_RECEIVED)]
    interval = request.app.state.config.tracking_interval_seconds
    return render(
        request,
        "pipeline.html",
        {
            "columns": columns,
            "close_reasons": CLOSE_REASONS,
            "last_checked_at": board.last_checked_at,
            "tracking_interval_minutes": interval // 60 if interval else None,
        },
    )


@router.get("/outreach/{outreach_id:int}")
def detail(request: Request, services: ServicesDep, outreach_id: OutreachId) -> Response:
    detail = services.outreach.detail(outreach_id)  # NotFound -> 404 page
    return render(request, "outreach_detail.html", {"detail": detail, "close_reasons": CLOSE_REASONS})


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


@router.post("/outreach/{outreach_id:int}/sent")
def mark_sent(
    request: Request, services: ServicesDep, outreach_id: OutreachId, next_url: NextField = "/drafts"
) -> Response:
    return _act(
        request,
        lambda: services.outreach.mark_sent(outreach_id),
        success="Marked as sent.",
        next_url=next_url,
    )


@router.post("/outreach/{outreach_id:int}/replied")
def mark_replied(
    request: Request, services: ServicesDep, outreach_id: OutreachId, next_url: NextField = "/pipeline"
) -> Response:
    return _act(
        request,
        lambda: services.outreach.mark_replied(outreach_id),
        success="Marked as replied.",
        next_url=next_url,
    )


@router.post("/outreach/{outreach_id:int}/deck-received")
def mark_deck_received(
    request: Request, services: ServicesDep, outreach_id: OutreachId, next_url: NextField = "/pipeline"
) -> Response:
    return _act(
        request,
        lambda: services.outreach.mark_deck_received(outreach_id),
        success="Marked as deck received.",
        next_url=next_url,
    )


@router.post("/outreach/{outreach_id:int}/close")
def close(
    request: Request,
    services: ServicesDep,
    outreach_id: OutreachId,
    reason: Annotated[CloseReason, Form()] = CloseReason.SKIPPED,
    next_url: NextField = "/pipeline",
) -> Response:
    return _act(
        request,
        lambda: services.outreach.close(outreach_id, reason),
        success=f"Closed ({reason.value.replace('_', ' ')}).",
        next_url=next_url,
    )


@router.post("/outreach/{outreach_id:int}/discard-email")
def discard_email(
    request: Request, services: ServicesDep, outreach_id: OutreachId, next_url: NextField = "/drafts"
) -> Response:
    return _act(
        request,
        lambda: services.outreach.discard_email(outreach_id),
        success="Email discarded; the founder is back in Needs email.",
        next_url=next_url,
    )


@router.post("/outreach/{outreach_id:int}/email")
def set_email(
    request: Request,
    services: ServicesDep,
    outreach_id: OutreachId,
    email: Annotated[str, Form(max_length=254)] = "",
    next_url: NextField = "/needs-email",
) -> Response:
    return _act(
        request,
        lambda: services.outreach.set_manual_email(outreach_id, email),
        success="Email saved and draft created (see Drafts).",
        next_url=next_url,
    )


@router.post("/outreach/{outreach_id:int}/choose-email")
def choose_email(
    request: Request,
    services: ServicesDep,
    outreach_id: OutreachId,
    email: Annotated[str, Form(max_length=254)] = "",
    next_url: NextField = "/drafts",
) -> Response:
    return _act(
        request,
        lambda: services.outreach.choose_email(outreach_id, email),
        success="Address chosen and the draft updated (see Drafts).",
        next_url=next_url,
    )


@router.post("/outreach/{outreach_id:int}/draft-anyway")
def draft_anyway(
    request: Request, services: ServicesDep, outreach_id: OutreachId, next_url: NextField = "/excluded"
) -> Response:
    target = safe_next(next_url)
    try:
        record = services.outreach.draft_anyway(outreach_id)
    except ServiceError as exc:
        return redirect(request, target, exc.message, "error")
    if record.status is S.DRAFTED:
        return redirect(request, target, "Exclusion overridden: draft created (see Drafts).", "success")
    return redirect(request, target, "Exclusion overridden: no email known yet (see Needs email).", "success")


@router.post("/outreach/{outreach_id:int}/forget")
def forget(
    request: Request,
    services: ServicesDep,
    outreach_id: OutreachId,
    confirm: Annotated[str, Form(max_length=10)] = "",
) -> Response:
    """Hard delete (UK GDPR erasure). Needs the explicit confirmation checkbox."""
    if confirm != "yes":
        return redirect(
            request, f"/outreach/{outreach_id}", "Tick the confirmation box to forget this founder.", "error"
        )
    try:
        services.outreach.forget_outreach_founder(outreach_id)
    except ServiceError as exc:
        return redirect(request, "/", exc.message, "error")
    return redirect(request, "/", "Founder forgotten: every record about them was deleted.", "success")
