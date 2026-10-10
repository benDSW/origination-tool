"""EvertraceAdapter: mapping, pagination, since-filtering, catalog operations. Synthetic data only."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx

from app.domain.models import (
    CompanyRef,
    NewSearchSpec,
    SavedSearch,
    SearchFilter,
    SearchVocabulary,
    Signal,
)
from app.domain.ports import IntegrationError, NotFoundError, SearchCatalog, SignalSource
from app.integrations.evertrace import EvertraceAdapter
from app.integrations.evertrace import adapter as adapter_module
from app.integrations.evertrace.adapter import (
    map_cities,
    map_signal,
    map_vocabulary,
    parse_epoch_ms,
    parse_linkedin_slug,
    registry_url,
    select_current_company,
    website_domain,
)
from app.integrations.evertrace.filters import to_epoch_ms
from app.integrations.evertrace.transport import ToolError
from tests.fakes import utc
from tests.integrations.evertrace.mcp_fake import FAKE_KEY, MCP_URL, FakeClock, FakeMcpServer

SINCE = utc(2026, 10, 1)
SINCE_MS = to_epoch_ms(SINCE)


# --------------------------------------------------------------------------- #
# Synthetic payload builders
# --------------------------------------------------------------------------- #


def compact(n: int, *, discovered: datetime | None = None, **extra: Any) -> dict[str, Any]:
    at = discovered or utc(2026, 10, 2, 9, 0)
    return {
        "id": f"sig_syn{n:04d}",
        "firstName": f"Sam{n}",
        "lastName": "Synthetic",
        "score": 8,
        "city": "Leeds",
        "country": "United Kingdom",
        "nationality": "British",
        "age": "25-34",
        "gender": "woman",
        "profileAccuracy": 0.9,
        "discoveredAt": to_epoch_ms(at),
        "linkedinIdStr": f"sam-synthetic-{n}",
        "source": "linkedin",
        "signalType": "New Company",
        "industries": ["Robotics"],
        "profileTags": ["Technical"],
        **extra,
    }


def experience(
    title: str,
    company: str | None,
    *,
    index: int = 0,
    end: str | None = None,
    website: str | None = "https://www.example-robotics.test",
    source_url: str | None = "find-and-update.company-information.service.gov.uk/company/00000001",
    entity_name: str | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "location": "Leeds",
        "companyName": company,
        "indexOrder": index,
        "startDate": "2026-01-01",
        "endDate": end,
        "entity": {
            "source": "companies_house",
            "name": entity_name,
            "websiteUrl": website,
            "sourceUrl": source_url,
            "logoUrl": None,
            "employeeCount": 3,
        },
    }


def detail(n: int, *, discovered: datetime | None = None, **extra: Any) -> dict[str, Any]:
    base = compact(n, discovered=discovered)
    base.update(
        {
            "email": f"sam{n}@example-robotics.test",
            "summary": "Synthetic founder building synthetic robots.",
            "experiences": [experience("Co-Founder & CEO", f"Synthetic Robotics {n} Ltd")],
            "educations": [],
            "region": {"name": "Yorkshire and the Humber"},
            "somethingElse": {"ignored": True},
        }
    )
    base.update(extra)
    return base


class FakeTransport:
    """Routes tool calls to handlers and records them (no network)."""

    def __init__(self, **handlers: Callable[[dict[str, Any]], Any]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        return self.handlers[name](dict(arguments))

    def close(self) -> None:
        self.closed = True

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def signal_source(
    pages: list[list[dict[str, Any]]], details: dict[str, dict[str, Any]] | None = None, **kw: Any
):
    details = details if details is not None else {}

    def list_signals(args: dict[str, Any]) -> dict[str, Any]:
        page = args["page"]
        data = pages[page - 1] if page <= len(pages) else []
        return {"data": data, "meta": {"page": page, "limit": kw.get("meta_limit", args["limit"])}}

    def get_signal(args: dict[str, Any]) -> dict[str, Any]:
        sid = args["id"]
        if sid in details:
            return details[sid]
        n = int(sid.removeprefix("sig_syn"))
        return detail(n)

    return FakeTransport(list_signals=list_signals, get_signal=get_signal)


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #


def test_adapter_implements_both_ports() -> None:
    adapter = EvertraceAdapter("unused", transport=FakeTransport())
    assert isinstance(adapter, SearchCatalog)
    assert isinstance(adapter, SignalSource)
    assert "unused" not in repr(adapter)
    adapter.close()


def test_close_on_transport_without_close() -> None:
    class Bare:
        def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
            return None

    EvertraceAdapter("k", transport=Bare()).close()


# --------------------------------------------------------------------------- #
# Pure mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sam-synthetic-1", "sam-synthetic-1"),
        ("ACwAAA_x.y%20z", "ACwAAA_x.y%20z"),
        ("  padded-slug  ", "padded-slug"),
        ("a", None),
        ("-leading-dash", None),
        ("https://www.linkedin.com/in/someone", None),
        ("has space", None),
        ("x" * 101, None),
        ("slug/../../etc", None),
        (None, None),
        (12345, None),
    ],
)
def test_linkedin_slug_validation(raw: object, expected: str | None) -> None:
    assert parse_linkedin_slug(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Example-Robotics.test/about?x=1", "example-robotics.test"),
        ("http://shop.example.co.uk", "shop.example.co.uk"),
        ("example.io", "example.io"),
        ("www.example.io/path", "example.io"),
        ("ftp://example.io", None),
        ("javascript:alert(1)", None),
        ("https://127.0.0.1", None),
        ("https://localhost", None),
        ("https://exa mple.com", None),
        ("https://[::1", None),
        ("", None),
        (None, None),
    ],
)
def test_website_domain(raw: object, expected: str | None) -> None:
    assert website_domain(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "find-and-update.company-information.service.gov.uk/company/00000001",
            "https://find-and-update.company-information.service.gov.uk/company/00000001",
        ),
        ("https://registry.example.test/c/1", "https://registry.example.test/c/1"),
        ("http://registry.example.test/c/1", None),
        ("javascript://registry.example.test/%0aalert(1)", None),
        ("https://user:pw@registry.example.test/c/1", None),
        ("https://registry.example.test/c/<script>", None),
        ("https://[::1", None),
        ("https://nodot/c/1", None),
        (None, None),
    ],
)
def test_registry_url(raw: object, expected: str | None) -> None:
    assert registry_url(raw) == expected


def test_parse_epoch_ms_variants() -> None:
    assert parse_epoch_ms(SINCE_MS) == SINCE
    assert parse_epoch_ms(float(SINCE_MS)) == SINCE
    assert parse_epoch_ms(str(SINCE_MS)) == SINCE
    assert parse_epoch_ms("2026-10-01T00:00:00Z") == SINCE
    assert parse_epoch_ms("2026-10-01T00:00:00") == SINCE
    assert parse_epoch_ms("not a date") is None
    assert parse_epoch_ms(10**20) is None
    assert parse_epoch_ms(True) is None
    assert parse_epoch_ms(None) is None


def test_current_role_prefers_founder_title_over_index() -> None:
    exps = [
        experience("Advisor", "Advisory Co", index=0, website="advisory.test"),
        experience("Head of Product", "Old Employer", index=1, end="2025-12-31"),
        experience("Co-founder", "Founded Co", index=3, website="https://founded.test"),
        experience("Managing Director", "Other Co", index=2),
    ]
    company = select_current_company(exps)
    # Both "Co-founder" and "Managing Director" are founder-like; lower indexOrder wins.
    assert company == CompanyRef(
        name="Other Co",
        website_domain="example-robotics.test",
        registry_url="https://find-and-update.company-information.service.gov.uk/company/00000001",
    )


def test_current_role_falls_back_to_lowest_index_and_entity_name() -> None:
    exps = [
        experience("Engineer", None, index=5, entity_name="Entity Named Ltd", website=None, source_url=None),
        experience("Engineer", "Second Co", index=7),
        experience("Engineer", None, index=1, entity_name=None),  # no name at all -> ignored
        "not-a-dict",
    ]
    assert select_current_company(exps) == CompanyRef(name="Entity Named Ltd")


def test_no_current_role_gives_no_company() -> None:
    assert select_current_company([experience("CEO", "Past Co", end="2024-01-01")]) is None
    assert select_current_company(None) is None
    assert select_current_company([]) is None


def test_founder_title_word_boundaries() -> None:
    exps = [
        experience("Procurement Lead", "Proceos Ltd", index=0),  # "ceo" inside a word is not a CEO
        experience("CEO", "Real Ceo Ltd", index=9),
    ]
    assert select_current_company(exps).name == "Real Ceo Ltd"  # type: ignore[union-attr]


def test_map_signal_full() -> None:
    signal = map_signal(detail(1))
    assert signal == Signal(
        external_id="sig_syn0001",
        first_name="Sam1",
        last_name="Synthetic",
        email="sam1@example-robotics.test",
        linkedin_slug="sam-synthetic-1",
        city="Leeds",
        country="United Kingdom",
        gender="woman",
        score=8,
        signal_type="New Company",
        discovered_at=utc(2026, 10, 2, 9, 0),
        summary="Synthetic founder building synthetic robots.",
        industries=("Robotics",),
        company=CompanyRef(
            name="Synthetic Robotics 1 Ltd",
            website_domain="example-robotics.test",
            registry_url="https://find-and-update.company-information.service.gov.uk/company/00000001",
        ),
    )


def test_map_signal_falls_back_to_compact_and_tolerates_bad_types() -> None:
    sparse = {
        "id": "sig_syn0002",
        "email": None,
        "score": "seven",
        "city": {"name": "nested"},
        "industries": "not-a-list",
        "experiences": None,
        "linkedinIdStr": "https://evil.test/x",
    }
    signal = map_signal(sparse, compact(2, email="  sam2@example.test ", score="6"))
    assert signal is not None
    assert signal.email == "sam2@example.test"
    assert signal.score is None  # detail's non-numeric score is not replaced (detail wins when present)
    assert signal.city is None  # detail value present but malformed -> dropped, not guessed
    assert signal.industries == ()
    assert signal.linkedin_slug is None
    assert signal.company is None
    assert signal.discovered_at == utc(2026, 10, 2, 9, 0)


def test_map_signal_unusable_returns_none() -> None:
    assert map_signal({"firstName": "No Id", "discoveredAt": SINCE_MS}) is None
    assert map_signal({"id": "sig_x"}) is None


def test_map_signal_email_empty_is_none() -> None:
    assert map_signal(detail(3, email="   ")).email is None  # type: ignore[union-attr]


def test_map_vocabulary() -> None:
    vocab = map_vocabulary(
        {
            "statuses": [{"value": "New Company", "description": "d"}, {"value": "New Patent"}, {"x": 1}],
            "profileTags": [{"value": "Technical", "description": "d"}],
            "industries": ["Robotics", "Robotics", "BatteryTech", None],
            "countries": ["United Kingdom"],
            "regions": ["North West"],
            "ageRanges": ["25-34"],
            "educationLevels": ["PhD"],
            "genders": ["man", "woman"],
            "customerFocus": ["B2B", "B2C"],
            "scoreRange": {"min": 2, "max": 9},
            "unrelated": [1, 2, 3],
        }
    )
    assert vocab == SearchVocabulary(
        statuses=("New Company", "New Patent"),
        industries=("Robotics", "BatteryTech"),
        countries=("United Kingdom",),
        regions=("North West",),
        genders=("man", "woman"),
        profile_tags=("Technical",),
        age_ranges=("25-34",),
        education_levels=("PhD",),
        customer_focus=("B2B", "B2C"),
        score_min=2,
        score_max=9,
    )
    assert map_vocabulary({}) == SearchVocabulary()
    with pytest.raises(IntegrationError):
        map_vocabulary(["not", "an", "object"])


@pytest.mark.parametrize(
    "payload",
    [
        ["Leeds", "Leeds", "Leicester"],
        {"items": ["Leeds", {"name": "Leicester"}]},
        {"data": [{"city": "Leeds"}, {"value": "Leicester"}, {"other": 1}, 5]},
    ],
)
def test_map_cities_shapes(payload: object) -> None:
    assert map_cities(payload, 10) == ["Leeds", "Leicester"]


def test_map_cities_limit_and_unknown_shape() -> None:
    assert map_cities(["A", "B", "C"], 2) == ["A", "B"]
    assert map_cities({"weird": 1}, 5) == []


# --------------------------------------------------------------------------- #
# iter_signals: pagination, since filtering, detail fetch policy
# --------------------------------------------------------------------------- #


def test_iter_signals_paginates_until_short_page() -> None:
    full_page = [compact(i) for i in range(1, 101)]
    transport = signal_source([full_page, [compact(101), compact(102)]])
    adapter = EvertraceAdapter("k", transport=transport)
    filters = (SearchFilter(key="city", values=("!London",)), SearchFilter(key="location", values=("x",)))

    signals = list(adapter.iter_signals(filters, SINCE))

    assert len(signals) == 102
    assert all(isinstance(s, Signal) for s in signals)
    list_calls = [a for n, a in transport.calls if n == "list_signals"]
    assert [c["page"] for c in list_calls] == [1, 2]
    for call in list_calls:
        assert call["limit"] == 100
        assert call["created_after"] == str(SINCE_MS)
        assert call["city"] == ["!London"]
        assert "location" not in call
    assert transport.names().count("get_signal") == 102
    assert all(a == {"id": a["id"], "compact": False} for n, a in transport.calls if n == "get_signal")


def test_iter_signals_stops_on_empty_page() -> None:
    transport = signal_source([[compact(i) for i in range(1, 101)], []])
    assert len(list(EvertraceAdapter("k", transport=transport).iter_signals((), SINCE))) == 100
    assert transport.names().count("list_signals") == 2


def test_iter_signals_uses_server_page_limit() -> None:
    """If the server caps the page size (meta.limit), a page of that size is not the last."""
    transport = signal_source([[compact(1), compact(2)], [compact(3)]], meta_limit=2)
    signals = list(EvertraceAdapter("k", transport=transport).iter_signals((), SINCE))
    assert [s.external_id for s in signals] == ["sig_syn0001", "sig_syn0002", "sig_syn0003"]


def test_iter_signals_skips_old_and_duplicate_items_without_fetching() -> None:
    old = compact(1, discovered=utc(2026, 9, 30, 23, 59))
    boundary = compact(2, discovered=SINCE)
    transport = signal_source([[old, boundary, boundary, "garbage", {"no": "id"}]])
    adapter = EvertraceAdapter("k", transport=transport)

    signals = list(adapter.iter_signals((), SINCE))

    assert [s.external_id for s in signals] == ["sig_syn0002"]
    assert [a["id"] for n, a in transport.calls if n == "get_signal"] == ["sig_syn0002"]


def test_iter_signals_uses_detail_time_when_compact_lacks_it() -> None:
    item_new = compact(1)
    item_new.pop("discoveredAt")
    item_old = compact(2)
    item_old.pop("discoveredAt")
    details = {"sig_syn0002": detail(2, discovered=utc(2026, 1, 1))}
    transport = signal_source([[item_new, item_old]], details)
    signals = list(EvertraceAdapter("k", transport=transport).iter_signals((), SINCE))
    assert [s.external_id for s in signals] == ["sig_syn0001"]


def test_iter_signals_skips_permanently_unusable_detail(caplog: pytest.LogCaptureFixture) -> None:
    item = compact(1)
    item.pop("discoveredAt")
    details = {"sig_syn0001": {"id": "sig_syn0001"}}
    transport = signal_source([[item, compact(2)]], details)
    with caplog.at_level(logging.WARNING):
        signals = list(EvertraceAdapter("k", transport=transport).iter_signals((), SINCE))
    assert [s.external_id for s in signals] == ["sig_syn0002"]
    assert "sig_syn0001" in caplog.text


def test_iter_signals_detail_failure_raises_so_watermark_is_not_advanced() -> None:
    def get_signal(args: dict[str, Any]) -> Any:
        if args["id"] == "sig_syn0002":
            raise IntegrationError("evertrace", "timed out", retryable=True)
        return detail(int(args["id"].removeprefix("sig_syn")))

    transport = FakeTransport(
        list_signals=lambda a: {"data": [compact(1), compact(2), compact(3)], "meta": {"limit": 100}},
        get_signal=get_signal,
    )
    yielded: list[Signal] = []
    with pytest.raises(IntegrationError) as info:
        for s in EvertraceAdapter("k", transport=transport).iter_signals((), SINCE):
            yielded.append(s)
    assert [s.external_id for s in yielded] == ["sig_syn0001"]
    assert "sig_syn0002" in str(info.value)
    assert info.value.retryable is True
    assert str(info.value).count("[evertrace]") == 1


def test_iter_signals_non_object_detail_raises() -> None:
    transport = signal_source([[compact(1)]], {"sig_syn0001": ["not", "a", "dict"]})  # type: ignore[dict-item]
    with pytest.raises(IntegrationError):
        list(EvertraceAdapter("k", transport=transport).iter_signals((), SINCE))


def test_iter_signals_page_cap(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(adapter_module, "MAX_PAGES", 3)
    counter = iter(range(1, 10_000))

    def list_signals(args: dict[str, Any]) -> dict[str, Any]:
        return {"data": [compact(next(counter)) for _ in range(100)], "meta": {"limit": 100}}

    transport = FakeTransport(list_signals=list_signals, get_signal=lambda a: detail(int(a["id"][7:])))
    with caplog.at_level(logging.ERROR):
        signals = list(EvertraceAdapter("k", transport=transport).iter_signals((), SINCE))
    assert len(signals) == 300
    assert transport.names().count("list_signals") == 3
    assert "cap" in caplog.text


def test_iter_signals_rejects_naive_since() -> None:
    adapter = EvertraceAdapter("k", transport=signal_source([[]]))
    with pytest.raises(ValueError):
        list(adapter.iter_signals((), datetime(2026, 10, 1)))  # noqa: DTZ001


def test_iter_signals_combines_saved_created_after() -> None:
    later = to_epoch_ms(utc(2026, 10, 3))
    transport = signal_source([[]])
    filters = (SearchFilter(key="created_after", operator="eq", values=(str(later),)),)
    list(EvertraceAdapter("k", transport=transport).iter_signals(filters, SINCE))
    assert transport.calls[0][1]["created_after"] == str(later)


# --------------------------------------------------------------------------- #
# SearchCatalog
# --------------------------------------------------------------------------- #

SEARCH_PAYLOAD = {
    "id": "sch_syn1",
    "title": "Synthetic North",
    "emoji": None,
    "filters": [
        {"key": "city", "operator": "in", "value": "!London"},
        {"key": "score", "operator": "eq", "value": "7"},
        {"key": "status", "operator": "in", "value": "New Company|New Patent"},
        {"key": "country", "operator": "in", "value": "United Kingdom"},
        "junk",
    ],
}


def test_list_searches() -> None:
    transport = FakeTransport(
        list_searches=lambda a: {
            "items": [
                {"id": "sch_a", "title": "A", "emoji": None, "createdAt": 1, "shareeCount": 0},
                {"id": "sch_b", "title": None},
                {"title": "no id"},
                "junk",
            ]
        }
    )
    searches = EvertraceAdapter("k", transport=transport).list_searches()
    assert searches == [
        SavedSearch(external_id="sch_a", title="A"),
        SavedSearch(external_id="sch_b", title="sch_b"),
    ]
    assert transport.calls == [("list_searches", {})]


def test_get_search_maps_filters() -> None:
    transport = FakeTransport(get_search=lambda a: SEARCH_PAYLOAD)
    search = EvertraceAdapter("k", transport=transport).get_search(" sch_syn1 ")
    assert transport.calls == [("get_search", {"id": "sch_syn1", "compact": False})]
    assert search == SavedSearch(
        external_id="sch_syn1",
        title="Synthetic North",
        filters=(
            SearchFilter(key="city", operator="in", values=("!London",)),
            SearchFilter(key="score", operator="eq", values=("7",)),
            SearchFilter(key="status", operator="in", values=("New Company", "New Patent")),
            SearchFilter(key="country", operator="in", values=("United Kingdom",)),
        ),
    )


def test_get_search_not_found() -> None:
    def missing(args: dict[str, Any]) -> Any:
        raise ToolError("get_search", "Search not found")

    adapter = EvertraceAdapter("k", transport=FakeTransport(get_search=missing))
    with pytest.raises(NotFoundError):
        adapter.get_search("sch_missing")
    with pytest.raises(NotFoundError):
        adapter.get_search("  ")


def test_get_search_other_tool_error_propagates() -> None:
    def broken(args: dict[str, Any]) -> Any:
        raise ToolError("get_search", "Internal problem")

    with pytest.raises(ToolError):
        EvertraceAdapter("k", transport=FakeTransport(get_search=broken)).get_search("sch_x")


@pytest.mark.parametrize("payload", [["not-an-object"], {"title": "no id"}])
def test_get_search_malformed_payload(payload: object) -> None:
    with pytest.raises(IntegrationError):
        EvertraceAdapter("k", transport=FakeTransport(get_search=lambda a: payload)).get_search("sch_x")


def test_create_search_sends_encoded_filters() -> None:
    transport = FakeTransport(
        create_search=lambda a: {**SEARCH_PAYLOAD, "id": "sch_new", "title": a["title"]}
    )
    spec = NewSearchSpec(
        title="  Synthetic North  ",
        filters=(
            SearchFilter(key="status", values=("New Company", "New Patent")),
            SearchFilter(key="score", operator="eq", values=("7",)),
        ),
    )
    created = EvertraceAdapter("k", transport=transport).create_search(spec)
    assert transport.calls == [
        (
            "create_search",
            {
                "title": "Synthetic North",
                "filters": [
                    {"key": "status", "operator": "in", "value": "New Company|New Patent"},
                    {"key": "score", "operator": "eq", "value": "7"},
                ],
            },
        )
    ]
    assert created.external_id == "sch_new"
    assert created.title == "Synthetic North"


def test_create_search_falls_back_to_spec_filters() -> None:
    transport = FakeTransport(create_search=lambda a: {"id": "sch_new", "title": a["title"]})
    spec = NewSearchSpec(title="T", filters=(SearchFilter(key="country", values=("United Kingdom",)),))
    assert EvertraceAdapter("k", transport=transport).create_search(spec).filters == spec.filters


@pytest.mark.parametrize(
    "spec",
    [
        NewSearchSpec(title="x" * 51, filters=()),
        NewSearchSpec(title="   ", filters=()),
        NewSearchSpec(title="ok", filters=(SearchFilter(key="fullname", values=("Someone",)),)),
        NewSearchSpec(title="ok", filters=(SearchFilter(key="city", values=("A|B",)),)),
    ],
)
def test_create_search_validation_happens_before_calling(spec: NewSearchSpec) -> None:
    transport = FakeTransport()
    with pytest.raises(ValueError):
        EvertraceAdapter("k", transport=transport).create_search(spec)
    assert transport.calls == []


def test_vocabulary_calls_metadata() -> None:
    transport = FakeTransport(get_signals_metadata=lambda a: {"genders": ["man", "woman"]})
    assert EvertraceAdapter("k", transport=transport).vocabulary().genders == ("man", "woman")
    assert transport.calls == [("get_signals_metadata", {})]


def test_search_cities_validates_and_clamps() -> None:
    transport = FakeTransport(search_cities=lambda a: {"items": [{"name": "Leeds"}]})
    adapter = EvertraceAdapter("k", transport=transport)
    assert adapter.search_cities("  Lee ", limit=500) == ["Leeds"]
    assert transport.calls == [("search_cities", {"search": "Lee", "limit": 50})]
    assert adapter.search_cities("") == []
    assert adapter.search_cities("x" * 101) == []
    adapter.search_cities("Lee", limit=0)
    assert transport.calls[-1][1]["limit"] == 1
    assert len(transport.calls) == 2


# --------------------------------------------------------------------------- #
# End to end over the real MCP transport (respx-mocked)
# --------------------------------------------------------------------------- #


@pytest.fixture
def server() -> FakeMcpServer:
    return FakeMcpServer(
        use_sse=True,
        tools={
            "list_signals": lambda a: {
                "data": [compact(1), compact(2)],
                "meta": {"page": a["page"], "limit": 100},
            },
            "get_signal": lambda a: detail(int(a["id"].removeprefix("sig_syn"))),
            "get_search": lambda a: SEARCH_PAYLOAD,
        },
    )


@pytest.fixture
def e2e_adapter(server: FakeMcpServer, http_client: httpx.Client) -> Iterator[EvertraceAdapter]:
    clock = FakeClock()
    with respx.mock(assert_all_called=False) as router:
        router.post(MCP_URL).mock(side_effect=server)
        yield EvertraceAdapter(
            FAKE_KEY, http_client=http_client, sleep=clock.sleep, monotonic=clock.monotonic
        )


def test_end_to_end_iter_signals(e2e_adapter: EvertraceAdapter, server: FakeMcpServer) -> None:
    search = e2e_adapter.get_search("sch_syn1")
    signals = list(e2e_adapter.iter_signals(search.filters, SINCE))
    assert [s.external_id for s in signals] == ["sig_syn0001", "sig_syn0002"]
    assert signals[0].company is not None
    list_args = [a for n, a in server.tool_calls() if n == "list_signals"][0]
    assert list_args == {
        "city": ["!London"],
        "score": "7",
        "status": ["New Company", "New Patent"],
        "country": ["United Kingdom"],
        "created_after": str(SINCE_MS),
        "page": 1,
        "limit": 100,
    }


def test_end_to_end_errors_never_contain_the_key(
    e2e_adapter: EvertraceAdapter, server: FakeMcpServer
) -> None:
    server.tools["get_signal"] = lambda a: httpx.Response(401, text=f"bad key {FAKE_KEY}")
    with pytest.raises(IntegrationError) as info:
        list(e2e_adapter.iter_signals((), SINCE))
    assert info.value.retryable is False
    assert FAKE_KEY not in str(info.value)
    assert FAKE_KEY not in repr(info.value)
    assert info.value.__cause__ is None


def test_iter_signals_without_meta_assumes_requested_limit() -> None:
    transport = FakeTransport(list_signals=lambda a: {"data": [compact(1)]}, get_signal=lambda a: detail(1))
    assert len(list(EvertraceAdapter("k", transport=transport).iter_signals((), SINCE))) == 1
    assert transport.names().count("list_signals") == 1
