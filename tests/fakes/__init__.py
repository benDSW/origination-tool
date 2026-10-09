"""In-memory fakes for every integration port.

Services are tested against these (no network). Each fake records its calls so
tests can assert on behaviour (e.g. "RocketReach was not called twice").
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.models import (
    EmailInteraction,
    EmailLookupResult,
    InboxEvent,
    KnownEntities,
    LookupOutcome,
    NewSearchSpec,
    SavedSearch,
    SearchFilter,
    SearchVocabulary,
    Signal,
)
from app.domain.ports import IntegrationError, NotFoundError


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def make_signal(n: int = 1, **overrides: object) -> Signal:
    """Synthetic founder signal. Never use real people's data in tests."""
    from app.domain.models import CompanyRef

    base: dict[str, object] = {
        "external_id": f"sig_test{n:04d}",
        "first_name": f"Alex{n}",
        "last_name": "Testfounder",
        "email": None,
        "linkedin_slug": f"alex-testfounder-{n}",
        "city": "Manchester",
        "country": "United Kingdom",
        "gender": "Woman",
        "score": 8,
        "signal_type": "New Company",
        "discovered_at": utc(2026, 10, 1, 9, 0),
        "summary": f"Building Example{n} Limited, B2B software.",
        "industries": ("Application Software",),
        "company": CompanyRef(name=f"Example{n} Limited", website_domain=None, registry_url=None),
    }
    base.update(overrides)
    return Signal(**base)  # type: ignore[arg-type]


@dataclass
class FakeSearchCatalog:
    searches: list[SavedSearch] = field(default_factory=list)
    vocab: SearchVocabulary = field(
        default_factory=lambda: SearchVocabulary(
            statuses=("New Company", "Stealth Position"),
            industries=("BatteryTech", "Application Software"),
            countries=("United Kingdom",),
            genders=("man", "woman"),
        )
    )
    created: list[NewSearchSpec] = field(default_factory=list)
    fail: bool = False

    def list_searches(self) -> list[SavedSearch]:
        if self.fail:
            raise IntegrationError("evertrace", "unavailable")
        return list(self.searches)

    def get_search(self, external_id: str) -> SavedSearch:
        if self.fail:
            raise IntegrationError("evertrace", "unavailable")
        for s in self.searches:
            if s.external_id == external_id:
                return s
        raise NotFoundError(external_id)

    def create_search(self, spec: NewSearchSpec) -> SavedSearch:
        self.created.append(spec)
        saved = SavedSearch(external_id=f"sch_new{len(self.created)}", title=spec.title, filters=spec.filters)
        self.searches.append(saved)
        return saved

    def vocabulary(self) -> SearchVocabulary:
        return self.vocab

    def search_cities(self, query: str, limit: int = 20) -> list[str]:
        return [c for c in ("Manchester", "Leeds", "London") if query.lower() in c.lower()][:limit]


@dataclass
class FakeSignalSource:
    """Map of search filters -> signals. Filters are matched by identity of the tuple."""

    by_filters: dict[tuple[SearchFilter, ...], list[Signal]] = field(default_factory=dict)
    calls: list[tuple[tuple[SearchFilter, ...], datetime]] = field(default_factory=list)
    fail_for: set[tuple[SearchFilter, ...]] = field(default_factory=set)

    def iter_signals(self, filters: tuple[SearchFilter, ...], since: datetime) -> Iterator[Signal]:
        self.calls.append((filters, since))
        if filters in self.fail_for:
            raise IntegrationError("evertrace", "boom")
        for s in self.by_filters.get(filters, []):
            if s.discovered_at >= since:
                yield s


@dataclass
class FakeEmailFinder:
    results: dict[str, EmailLookupResult] = field(default_factory=dict)  # by linkedin slug
    calls: list[str] = field(default_factory=list)
    credits: int | None = 100
    raise_for: set[str] = field(default_factory=set)

    def find_email(
        self, *, linkedin_slug: str, full_name: str, company_domain: str | None
    ) -> EmailLookupResult:
        self.calls.append(linkedin_slug)
        if linkedin_slug in self.raise_for:
            raise IntegrationError("rocketreach", "timeout")
        return self.results.get(linkedin_slug, EmailLookupResult(outcome=LookupOutcome.NOT_FOUND))

    def remaining_credits(self) -> int | None:
        return self.credits


@dataclass
class FakeCrm:
    known: KnownEntities = field(default_factory=KnownEntities)
    interactions: dict[str, list[EmailInteraction]] = field(default_factory=dict)  # by email
    fail: bool = False

    def known_entities(self) -> KnownEntities:
        if self.fail:
            raise IntegrationError("affinity", "unavailable")
        return self.known

    def email_interactions(self, email: str, since: datetime) -> list[EmailInteraction]:
        return [i for i in self.interactions.get(email, []) if i.occurred_at >= since]


@dataclass
class FakeInbox:
    events: dict[str, list[InboxEvent]] = field(default_factory=dict)  # by email
    calls: list[str] = field(default_factory=list)
    fail: bool = False

    def events_for(self, email: str, since: datetime) -> list[InboxEvent]:
        self.calls.append(email)
        if self.fail:
            raise IntegrationError("inbox", "unavailable")
        return sorted(
            (e for e in self.events.get(email, []) if e.occurred_at >= since), key=lambda e: e.occurred_at
        )
