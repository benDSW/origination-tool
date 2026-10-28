"""Jinja2 rendering and the POST-redirect-GET helpers.

Autoescaping is ON for every template, whatever its extension. Founder names,
summaries and company names come from Evertrace and are untrusted: templates
must never mark them ``|safe``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import jinja2
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates

from app.domain.models import FounderRecord
from app.web.auth import ADMIN_USER
from app.web.csrf import CSRF_FIELD, ensure_token
from app.web.flash import FLASH_COOKIE, Flash, FlashStore, Level

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Only these link shapes are ever rendered as clickable links.
_LINKEDIN = re.compile(r"^https://www\.linkedin\.com/in/[A-Za-z0-9_%.\-]{1,100}/?$")
_COMPANIES_HOUSE = re.compile(
    r"^https://find-and-update\.company-information\.service\.gov\.uk/[A-Za-z0-9_./\-]{0,200}$"
)


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def label(value: object) -> str:
    """'deck_received' -> 'deck received' (enums or plain strings)."""
    if value is None:
        return "-"
    text = value.value if isinstance(value, Enum) else str(value)
    return str(text).replace("_", " ")


def linkedin_link(founder: FounderRecord) -> str | None:
    url = founder.linkedin_url
    return url if url and _LINKEDIN.fullmatch(url) else None


def companies_house_link(founder: FounderRecord) -> str | None:
    url = founder.company_registry_url
    return url if url and _COMPANIES_HOUSE.fullmatch(url) else None


STATIC_DIR = Path(__file__).resolve().parent / "static"
_static_versions: dict[str, tuple[float, str]] = {}


def static_url(name: str) -> str:
    """'/static/app.js?v=<content hash>': a changed file gets a new URL, so browsers
    never keep running a stale copy. Re-hashed only when the file's mtime changes."""
    path = STATIC_DIR / name
    mtime = path.stat().st_mtime
    cached = _static_versions.get(name)
    if cached is None or cached[0] != mtime:
        cached = (mtime, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
        _static_versions[name] = cached
    return f"/static/{name}?v={cached[1]}"


def _build_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["dt"] = format_dt
    env.filters["label"] = label
    env.globals["linkedin_link"] = linkedin_link
    env.globals["companies_house_link"] = companies_house_link
    env.globals["static_url"] = static_url
    env.globals["CSRF_FIELD"] = CSRF_FIELD
    return env


templates = Jinja2Templates(env=_build_env())


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, *, status_code: int = 200
) -> Response:
    """Render a page with the common context (CSRF token, flashes, current path)."""
    store: FlashStore = request.app.state.flash
    flashes = store.read(request)
    ctx: dict[str, Any] = {
        "csrf_token": ensure_token(request.session),
        "flashes": flashes,
        "path": request.url.path,
        "authenticated": request.session.get("user") == ADMIN_USER,
    }
    ctx.update(context or {})
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if FLASH_COOKIE in request.cookies:
        store.clear(response)
    return response


def redirect(
    request: Request, url: str, message: str | None = None, level: Level = "info"
) -> RedirectResponse:
    """303 See Other (the PRG pattern), optionally carrying one flash message."""
    response = RedirectResponse(url, status_code=303)
    if message:
        store: FlashStore = request.app.state.flash
        store.set(response, [Flash(level=level, text=message)])
    return response
