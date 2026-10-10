"""Saved searches (R18) and the new-search form (R19)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Form, Path, Request
from starlette.responses import Response

from app.domain.models import LocationPolicy, SearchFilter, SearchVocabulary
from app.services import ExternalServiceError, ServiceError, ValidationFailed, city_filter
from app.web.deps import MAX_ID, ServicesDep
from app.web.rendering import redirect, render
from app.web.routes.overview import run_in_background

router = APIRouter()

SearchId = Annotated[int, Path(ge=1, le=MAX_ID)]
MAX_FORM_VALUES = 60
MAX_FORM_VALUE_LEN = 100
MAX_TITLE_FORM_LEN = 200
MAX_CITY_FIELD_LEN = 2000
MAX_CITIES = 50
DEFAULT_COUNTRY = "United Kingdom"

# Form field -> (Evertrace filter key, SearchVocabulary attribute, label)
MULTI_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("status", "statuses", "Signal status"),
    ("industry", "industries", "Industries"),
    ("country", "countries", "Countries"),
    ("gender", "genders", "Gender"),
    ("profile_tags", "profile_tags", "Profile tags"),
)

MultiField = Annotated[list[str] | None, Form()]


@dataclass
class NewSearchForm:
    """What the user typed, kept so an invalid form re-renders with their input."""

    title: str = ""
    selected: dict[str, list[str]] = field(default_factory=lambda: {"country": [DEFAULT_COUNTRY]})
    min_score: str = ""
    city_include: str = ""
    city_exclude: str = ""
    location_policy: str = LocationPolicy.UK_OUTSIDE_LONDON.value
    lookback_days: str = "30"
    enabled: bool = True
    backfill: bool = True


def _split_cities(text: str) -> list[str]:
    return [c.strip() for c in text.split(",") if c.strip()][:MAX_CITIES]


def _clean_values(values: list[str] | None, problems: list[str], label: str) -> list[str]:
    out = [v.strip() for v in (values or []) if v and v.strip()]
    if len(out) > MAX_FORM_VALUES:
        problems.append(f"{label}: too many values selected")
        return []
    if any(len(v) > MAX_FORM_VALUE_LEN for v in out):
        problems.append(f"{label}: a value is too long")
        return []
    return out


def _build_filters(form: NewSearchForm, problems: list[str]) -> list[SearchFilter]:
    filters: list[SearchFilter] = []
    for key, _attr, _label in MULTI_FIELDS:
        values = form.selected.get(key, [])
        if values:
            filters.append(SearchFilter(key=key, operator="in", values=tuple(values)))
    if form.min_score:
        if form.min_score.isdigit() and 1 <= int(form.min_score) <= 10:
            filters.append(SearchFilter(key="score", operator="in", values=(str(int(form.min_score)),)))
        else:
            problems.append("Minimum score must be a whole number from 1 to 10")
    cities = city_filter(_split_cities(form.city_include), _split_cities(form.city_exclude))
    if cities is not None:
        filters.append(cities)
    return filters


def _render_new(
    request: Request,
    services_vocab: SearchVocabulary | None,
    form: NewSearchForm,
    *,
    problems: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    fields = []
    if services_vocab is not None:
        for key, attr, label_text in MULTI_FIELDS:
            options: tuple[str, ...] = getattr(services_vocab, attr)
            fields.append(
                {"name": key, "label": label_text, "options": options, "selected": form.selected.get(key, [])}
            )
    context = {
        "vocab": services_vocab,
        "fields": fields,
        "form": form,
        "problems": problems or [],
        "policies": list(LocationPolicy),
        "scores": list(range(1, 11)),
    }
    return render(request, "search_new.html", context, status_code=status_code)


# --------------------------------------------------------------------------- #
# List + local settings
# --------------------------------------------------------------------------- #


@router.get("/searches")
def searches(request: Request, services: ServicesDep) -> Response:
    return render(
        request,
        "searches.html",
        {"configs": services.searches.list_configs(), "policies": list(LocationPolicy)},
    )


@router.post("/searches/sync")
def sync_searches(request: Request, services: ServicesDep) -> Response:
    try:
        configs = services.searches.sync_catalog()
    except ServiceError as exc:
        return redirect(request, "/searches", exc.message, "error")
    return redirect(
        request,
        "/searches",
        f"Synced from Evertrace: {len(configs)} search(es). New ones start disabled.",
        "success",
    )


@router.post("/searches/{search_id:int}")
def update_search(
    request: Request,
    services: ServicesDep,
    search_id: SearchId,
    location_policy: Annotated[LocationPolicy, Form()],
    lookback_days: Annotated[int, Form(ge=1, le=730)],
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    try:
        config = services.searches.update_config(
            search_id, enabled=enabled, location_policy=location_policy, lookback_days=lookback_days
        )
    except ServiceError as exc:
        return redirect(request, "/searches", exc.message, "error")
    state = "enabled" if config.enabled else "disabled"
    return redirect(request, "/searches", f"Saved '{config.title}' ({state}).", "success")


@router.post("/searches/{search_id:int}/run")
def run_search(
    request: Request, services: ServicesDep, search_id: SearchId, background: BackgroundTasks
) -> Response:
    try:
        config = services.searches.get_config(search_id)
    except ServiceError as exc:
        return redirect(request, "/searches", exc.message, "error")
    background.add_task(run_in_background, services, config.id)
    return redirect(
        request, "/searches", f"Run started for '{config.title}'. See Runs for the result.", "success"
    )


# --------------------------------------------------------------------------- #
# New search
# --------------------------------------------------------------------------- #


@router.get("/searches/new")
def new_search_page(request: Request, services: ServicesDep) -> Response:
    try:
        vocab = services.searches.vocabulary()
    except ExternalServiceError as exc:
        return _render_new(request, None, NewSearchForm(), problems=[exc.message], status_code=502)
    return _render_new(request, vocab, NewSearchForm())


@router.post("/searches/new")
def create_search(
    request: Request,
    services: ServicesDep,
    background: BackgroundTasks,
    title: Annotated[str, Form(max_length=MAX_TITLE_FORM_LEN)] = "",
    status: MultiField = None,
    industry: MultiField = None,
    country: MultiField = None,
    gender: MultiField = None,
    profile_tags: MultiField = None,
    min_score: Annotated[str, Form(max_length=3)] = "",
    city_include: Annotated[str, Form(max_length=MAX_CITY_FIELD_LEN)] = "",
    city_exclude: Annotated[str, Form(max_length=MAX_CITY_FIELD_LEN)] = "",
    location_policy: Annotated[str, Form(max_length=40)] = LocationPolicy.UK_OUTSIDE_LONDON.value,
    lookback_days: Annotated[str, Form(max_length=5)] = "30",
    enabled: Annotated[bool, Form()] = False,
    backfill: Annotated[bool, Form()] = False,
) -> Response:
    problems: list[str] = []
    raw = {
        "status": status,
        "industry": industry,
        "country": country,
        "gender": gender,
        "profile_tags": profile_tags,
    }
    selected = {key: _clean_values(raw[key], problems, label_text) for key, _a, label_text in MULTI_FIELDS}
    form = NewSearchForm(
        title=title,
        selected=selected,
        min_score=min_score.strip(),
        city_include=city_include,
        city_exclude=city_exclude,
        location_policy=location_policy,
        lookback_days=lookback_days,
        enabled=enabled,
        backfill=backfill,
    )
    lookback = int(lookback_days) if lookback_days.strip().isdigit() else 0
    if not 1 <= lookback <= 730:
        problems.append("Lookback must be 1-730 days")
    filters = _build_filters(form, problems)

    try:
        vocab: SearchVocabulary | None = services.searches.vocabulary()
    except ExternalServiceError as exc:
        return _render_new(request, None, form, problems=[exc.message], status_code=502)
    if problems:
        return _render_new(request, vocab, form, problems=problems, status_code=400)

    try:
        config = services.searches.create_search(
            title, filters, location_policy=location_policy, lookback_days=lookback, enabled=enabled
        )
    except ValidationFailed as exc:
        return _render_new(request, vocab, form, problems=exc.problems, status_code=400)
    except ServiceError as exc:
        return _render_new(request, vocab, form, problems=[exc.message], status_code=502)

    message = f"Search '{config.title}' created on Evertrace."
    if backfill:
        background.add_task(run_in_background, services, config.id)
        message += f" Backfill of the last {config.lookback_days} days started (see Runs)."
    return redirect(request, "/searches", message, "success")
