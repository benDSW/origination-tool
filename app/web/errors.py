"""Error pages. They never echo exception text: only fixed wording, or a
``ServiceError.message`` (which services guarantee is user-safe)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from app.services import ExternalServiceError, InvalidAction, NotFound, ServiceError, ValidationFailed
from app.web.rendering import render

logger = logging.getLogger(__name__)

_HTTP_MESSAGES: dict[int, tuple[str, str]] = {
    400: ("Bad request", "The request could not be understood."),
    401: ("Please log in", "You need to log in to do that."),
    403: (
        "Forbidden",
        "The form had expired or was not sent from this dashboard. Reload the page and try again.",
    ),
    404: ("Not found", "That page or record does not exist."),
    405: ("Not allowed", "That action is not available here."),
    413: ("Too large", "The request was too large."),
    429: ("Too many attempts", "Too many attempts. Please wait and try again later."),
    503: ("Unavailable", "The dashboard is starting up. Try again in a moment."),
}


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def error_response(request: Request, status_code: int, message: str | None = None) -> Response:
    title, default = _HTTP_MESSAGES.get(status_code, ("Error", "Something went wrong."))
    text = message or default
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": text}, status_code=status_code)
    return render(request, "error.html", {"title": title, "message": text}, status_code=status_code)


def service_status(exc: ServiceError) -> int:
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, InvalidAction):
        return 409
    if isinstance(exc, ValidationFailed):
        return 400
    if isinstance(exc, ExternalServiceError):
        return 502
    return 400


async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    return error_response(request, exc.status_code)


async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    # Do not echo pydantic's details: they include the submitted values.
    return error_response(request, 400, "Some of the submitted values were invalid.")


async def _service_error(request: Request, exc: ServiceError) -> Response:
    return error_response(request, service_status(exc), exc.message)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(ServiceError, _service_error)  # type: ignore[arg-type]
