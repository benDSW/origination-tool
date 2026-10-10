"""SearchService (R18, R19): catalogue sync, local settings, validated search creation."""

from __future__ import annotations

import pytest

from app.domain.models import LocationPolicy, NewSearchSpec, SavedSearch, SearchFilter, SearchVocabulary
from app.domain.ports import IntegrationError
from app.services import ExternalServiceError, NotFound, ValidationFailed, city_filter
from app.services.searches import SearchService, validate_filters
from tests.fakes import FakeSearchCatalog
from tests.services.conftest import LONDON, NORTH, NOW, World

VOCAB = SearchVocabulary(
    statuses=("New Company", "Stealth Position"),
    industries=("BatteryTech", "Application Software"),
    countries=("United Kingdom",),
    genders=("man", "woman"),
    profile_tags=("Repeat Founder",),
    score_min=1,
    score_max=10,
)


def F(key: str, *values: str, operator: str = "in") -> SearchFilter:  # noqa: N802 - tiny builder
    return SearchFilter(key=key, operator=operator, values=values)


def _svc(world: World) -> SearchService:
    return world.services.searches


# --------------------------------------------------------------------------- #
# Sync + settings
# --------------------------------------------------------------------------- #


def test_sync_catalog_registers_searches_disabled(world: World) -> None:
    world.catalog.searches.extend([NORTH, LONDON])
    configs = _svc(world).sync_catalog()
    assert {c.external_id for c in configs} == {"sch_north", "sch_london"}
    assert all(not c.enabled and c.last_synced_at == NOW for c in configs)
    assert _svc(world).list_configs() == configs


def test_sync_keeps_local_settings(world: World) -> None:
    world.enable(NORTH, policy=LocationPolicy.LONDON_ALLOWED, lookback_days=7)
    _svc(world).sync_catalog()
    config = world.config(NORTH)
    assert (config.enabled, config.location_policy, config.lookback_days) == (
        True,
        LocationPolicy.LONDON_ALLOWED,
        7,
    )


def test_sync_failure_is_an_external_error(world: World) -> None:
    world.catalog.fail = True
    with pytest.raises(ExternalServiceError, match=r"\[evertrace\] unavailable"):
        _svc(world).sync_catalog()


def test_update_config(world: World) -> None:
    config = world.enable(NORTH, enabled=False)
    svc = _svc(world)

    updated = svc.update_config(
        config.id or 0, enabled=True, location_policy="london_allowed", lookback_days=90
    )

    assert (updated.enabled, updated.location_policy, updated.lookback_days) == (
        True,
        LocationPolicy.LONDON_ALLOWED,
        90,
    )
    assert svc.update_config(config.id or 0, enabled=False).enabled is False
    assert svc.get_config(config.id or 0).lookback_days == 90


@pytest.mark.parametrize(
    "kwargs",
    [{"location_policy": "anywhere"}, {"lookback_days": 0}, {"lookback_days": 731}, {"lookback_days": True}],
)
def test_update_config_rejects_bad_values(world: World, kwargs: dict[str, object]) -> None:
    config = world.enable(NORTH)
    with pytest.raises(ValidationFailed):
        _svc(world).update_config(config.id or 0, **kwargs)  # type: ignore[arg-type]
    assert world.config(NORTH).lookback_days == 30


def test_update_missing_search(world: World) -> None:
    with pytest.raises(NotFound):
        _svc(world).update_config(999, enabled=True)


def test_find_by_external_id(world: World) -> None:
    config = world.enable(NORTH)
    assert _svc(world).find_by_external_id("sch_north") == config
    assert _svc(world).find_by_external_id("sch_missing") is None


def test_vocabulary_and_city_search(world: World) -> None:
    svc = _svc(world)
    assert "BatteryTech" in svc.vocabulary().industries
    assert svc.search_cities("man") == ["Manchester"]
    assert svc.search_cities("   ") == []
    assert svc.search_cities("x" * 101) == []
    assert svc.search_cities("le\x00e") == ["Leeds"]


def test_vocabulary_and_city_errors_are_external_errors(world: World) -> None:
    class _Down(FakeSearchCatalog):
        def vocabulary(self) -> SearchVocabulary:
            raise IntegrationError("evertrace", "down")

        def search_cities(self, query: str, limit: int = 20) -> list[str]:
            raise IntegrationError("evertrace", "down")

    world.catalog = _Down()
    svc = world.rebuild().searches
    with pytest.raises(ExternalServiceError):
        svc.vocabulary()
    with pytest.raises(ExternalServiceError):
        svc.search_cities("Leeds")


# --------------------------------------------------------------------------- #
# Create (R19)
# --------------------------------------------------------------------------- #


def test_create_search_on_evertrace_then_registers_it(world: World) -> None:
    city = city_filter(include=["Leeds", " !York "], exclude=["!London", ""])
    assert city is not None

    config = _svc(world).create_search(
        "  North batteries ",
        [F("industry", "batterytech"), F("score", "7"), city, F("gender", "!man")],
        location_policy="london_allowed",
        lookback_days=14,
    )

    spec = world.catalog.created[0]
    assert spec.title == "North batteries"
    assert spec.filters == (
        F("industry", "BatteryTech"),
        F("score", "7"),
        F("city", "Leeds", "York", "!London"),
        F("gender", "!man"),
    )
    assert config.id is not None
    assert config.external_id == "sch_new1"
    assert (config.enabled, config.location_policy, config.lookback_days) == (
        True,
        LocationPolicy.LONDON_ALLOWED,
        14,
    )


def test_city_filter_builder() -> None:
    assert city_filter() is None
    assert city_filter(include=["  "]) is None
    built = city_filter(exclude=["London"])
    assert built is not None and built.values == ("!London",)


@pytest.mark.parametrize(
    ("title", "filters", "problem"),
    [
        ("", [F("industry", "BatteryTech")], "Title must be 1-50 characters"),
        ("x" * 51, [F("industry", "BatteryTech")], "Title must be 1-50 characters"),
        ("Ok", [], "Add at least one filter"),
        ("Ok", [F("salary", "high")], "Unknown filter 'salary'"),
        ("Ok", [F("industry", "BatteryTech"), F("industry", "BatteryTech")], "given more than once"),
        ("Ok", [F("industry", "BatteryTech", operator="regex")], "unsupported operator"),
        ("Ok", [F("industry", "Crypto")], "'Crypto' is not a valid industry"),
        ("Ok", [F("region", "North West")], "no options for 'region'"),
        ("Ok", [F("score", "11")], "Score must be a whole number from 1 to 10"),
        ("Ok", [F("score", "high")], "Score must be a whole number"),
        ("Ok", [F("city", "!")], "empty city name"),
        ("Ok", [F("city", "Leeds", "!leeds")], "both included and excluded"),
        ("Ok", [F("city", "Leeds|London")], "invalid value"),
        ("Ok", [F("city", "Le\neds")], "invalid value"),
        ("Ok", [F("city", "  ", "")], "has no values"),
        ("Ok", [F("industry", "BatteryTech", "Application Software", operator="eq")], "exactly one value"),
        ("Ok", [F("city", *[f"Town{i}" for i in range(51)])], "too many values"),
    ],
)
def test_create_search_validation(
    world: World, title: str, filters: list[SearchFilter], problem: str
) -> None:
    with pytest.raises(ValidationFailed) as info:
        _svc(world).create_search(title, filters)
    assert any(problem in p for p in info.value.problems), info.value.problems
    assert world.catalog.created == []


def test_create_search_rejects_bad_settings(world: World) -> None:
    with pytest.raises(ValidationFailed) as info:
        _svc(world).create_search(
            "Ok", [F("industry", "BatteryTech")], location_policy="moon", lookback_days=0
        )
    assert len(info.value.problems) == 2


def test_validate_filters_dedupes_values_and_uses_vocab_bounds() -> None:
    vocab = VOCAB.model_copy(update={"score_min": 5})
    cleaned = validate_filters([F("status", "new company", "New Company"), F("score", "05")], vocab)
    assert cleaned == (F("status", "New Company"), F("score", "5"))
    with pytest.raises(ValidationFailed, match="from 5 to 10"):
        validate_filters([F("score", "3")], vocab)


def test_unknown_key_is_not_echoed_unbounded() -> None:
    with pytest.raises(ValidationFailed) as info:
        validate_filters([F("k" * 500 + "\x07", "v")], VOCAB)
    assert len(info.value.problems[0]) < 80
    assert "\x07" not in info.value.message


class _RejectingCatalog(FakeSearchCatalog):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def create_search(self, spec: NewSearchSpec) -> SavedSearch:
        raise self.exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (IntegrationError("evertrace", "tool failed"), ExternalServiceError),
        (ValueError("Unsupported search filter key: 'x'"), ValidationFailed),
    ],
)
def test_create_search_adapter_failures(world: World, exc: Exception, expected: type[Exception]) -> None:
    world.catalog = _RejectingCatalog(exc)
    svc = world.rebuild().searches
    with pytest.raises(expected):
        svc.create_search("Ok", [F("industry", "BatteryTech")])
    assert world.store.list_search_configs() == []


def test_backfill_after_create(world: World) -> None:
    """R19: a new search can be backfilled straight away with only_search_id."""
    from app.domain.models import RunTrigger
    from tests.fakes import make_signal

    config = _svc(world).create_search("Batteries", [F("industry", "BatteryTech")])
    saved = world.catalog.searches[-1]
    world.signals.by_filters[saved.filters] = [make_signal(1, email="a@co1.example")]

    run = world.services.origination.run(RunTrigger.MANUAL, only_search_id=config.id)

    assert run.counts.drafted == 1
