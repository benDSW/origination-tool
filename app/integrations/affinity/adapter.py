"""Affinity implementation of the domain :class:`~app.domain.ports.CrmGateway` port.

Read-only by design: the tool keeps all pipeline state in its own database and
only *reads* Affinity to (a) dedupe new founders against the watched opportunity
lists and (b) detect sent/replied emails via Affinity's email sync.

Every Affinity payload is treated as untrusted and possibly incomplete: any key
may be missing, null or of the wrong type, and the adapter degrades to "less
information" rather than crashing. Nothing but domain DTOs leaves this module.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr

from app.domain.models import (
    EmailInteraction,
    InteractionDirection,
    KnownEntities,
    KnownEntity,
)
from app.domain.templating import normalise_email
from app.integrations.affinity.client import AffinityClient

logger = logging.getLogger(__name__)

# Affinity rejects interaction windows longer than one year; keep a day's margin.
MAX_INTERACTION_WINDOW = timedelta(days=364)
EMAIL_INTERACTION_TYPE = 3
_MAX_SUBJECT_LEN = 300

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _str(value: Any) -> str | None:
    """A stripped non-empty string, or None for anything else."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(moment: datetime) -> datetime:
    # Defensive: domain datetimes are aware, but treat a naive one as UTC rather than crash.
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    text = _str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _person_addresses(person: Any) -> set[str]:
    """Every lower-cased address on a v1 person object (primary_email + emails)."""
    if not isinstance(person, dict):
        return set()
    raw: list[Any] = [person.get("primary_email")]
    emails = person.get("emails")
    if isinstance(emails, list):
        raw.extend(emails)
    return {addr.lower() for addr in (_str(v) for v in raw) if addr}


def _domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


class AffinityAdapter:
    """Read-only :class:`CrmGateway` backed by the Affinity v1 + v2 APIs."""

    def __init__(
        self,
        client: AffinityClient,
        *,
        list_ids: Sequence[int],
        dsw_email_domains: Iterable[str],
        clock: Clock = _utc_now,
    ) -> None:
        self._client = client
        # Deduplicate while preserving order (origination first, then applicant).
        self._list_ids: tuple[int, ...] = tuple(dict.fromkeys(int(i) for i in list_ids))
        self._dsw_domains = frozenset(d.strip().lower().lstrip("@") for d in dsw_email_domains if d.strip())
        if not self._dsw_domains:
            raise ValueError("At least one DSW email domain is required to classify email direction")
        self._clock = clock

    @classmethod
    def from_api_key(
        cls,
        api_key: str | SecretStr,
        *,
        list_ids: Sequence[int],
        dsw_email_domains: Iterable[str],
        clock: Clock = _utc_now,
    ) -> AffinityAdapter:
        """Convenience for the container: build the client with production defaults."""
        return cls(
            AffinityClient(api_key), list_ids=list_ids, dsw_email_domains=dsw_email_domains, clock=clock
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ #
    # Dedupe snapshot
    # ------------------------------------------------------------------ #

    def known_entities(self) -> KnownEntities:
        entities: list[KnownEntity] = []
        for list_id in self._list_ids:
            list_name = self._list_name(list_id)
            for page in self._client.iter_v2_pages(
                f"/lists/{list_id}/list-entries",
                [("limit", 100), ("fieldIds", "companies"), ("fieldIds", "persons")],
                label="list-entries",
            ):
                for entry in _dicts(page.get("data")):
                    entity = self._to_known_entity(entry, list_name)
                    if entity is not None:
                        entities.append(entity)
        return KnownEntities(entities=tuple(entities))

    def _list_name(self, list_id: int) -> str:
        data = self._client.get_v2(f"/lists/{list_id}", label="list")
        return _str(data.get("name")) or f"List {list_id}"

    @staticmethod
    def _to_known_entity(entry: dict[str, Any], list_name: str) -> KnownEntity | None:
        entity = entry.get("entity")
        if not isinstance(entity, dict):
            return None
        entry_type = _str(entry.get("type"))
        name = _str(entity.get("name"))
        company_names: list[str] = []
        company_domains: list[str] = []
        person_names: list[str] = []

        if entry_type == "company":  # tolerate company lists too
            if name:
                company_names.append(name)
            if domain := _str(entity.get("domain")):
                company_domains.append(domain)
        elif entry_type == "person":
            if full := _full_name(entity.get("firstName"), entity.get("lastName")):
                person_names.append(full)

        for field in _dicts(entity.get("fields")):
            value = field.get("value")
            if not isinstance(value, dict):
                continue
            field_id = field.get("id")
            value_type = value.get("type")
            if field_id == "companies" or value_type in ("company-multi", "company"):
                for company in _field_items(value):
                    if company_name := _str(company.get("name")):
                        company_names.append(company_name)
                    if domain := _str(company.get("domain")):
                        company_domains.append(domain)
            elif field_id == "persons" or value_type in ("person-multi", "person"):
                for person in _field_items(value):
                    if full := _full_name(person.get("firstName"), person.get("lastName")):
                        person_names.append(full)

        if not (name or company_names or company_domains or person_names):
            return None  # nothing to match on (e.g. a fully redacted entry)
        opportunity_name = name or (company_names[0] if company_names else f"Entry {entry.get('id', '?')}")
        return KnownEntity(
            list_name=list_name,
            opportunity_name=opportunity_name,
            company_names=tuple(dict.fromkeys(company_names)),
            company_domains=tuple(dict.fromkeys(company_domains)),
            person_names=tuple(dict.fromkeys(person_names)),
        )

    # ------------------------------------------------------------------ #
    # Email interactions
    # ------------------------------------------------------------------ #

    def email_interactions(self, email: str, since: datetime) -> list[EmailInteraction]:
        address = normalise_email(email)
        if address is None:
            return []
        address = address.lower()
        person_id = self._find_person_id(address)
        if person_id is None:
            return []

        now = _as_utc(self._clock())
        start = max(_as_utc(since), now - MAX_INTERACTION_WINDOW)
        if start >= now:
            return []

        interactions: list[EmailInteraction] = []
        for item in self._client.iter_v1_pages(
            "/interactions",
            [
                ("type", EMAIL_INTERACTION_TYPE),
                ("person_id", person_id),
                ("start_time", _iso_z(start)),
                ("end_time", _iso_z(now)),
                ("page_size", 100),
            ],
            label="interactions",
            items_key="emails",
        ):
            interaction = self._to_interaction(item, address, start)
            if interaction is not None:
                interactions.append(interaction)
        interactions.sort(key=lambda i: i.occurred_at)
        return interactions

    def _find_person_id(self, address: str) -> int | None:
        """Exact (case-insensitive) address match; Affinity's ``term`` search is fuzzy."""
        data = self._client.get_v1("/persons", [("term", address), ("page_size", 10)], label="persons")
        for person in _dicts(data.get("persons")):
            if address in _person_addresses(person):
                person_id = person.get("id")
                if isinstance(person_id, int) and not isinstance(person_id, bool):
                    return person_id
        return None

    def _to_interaction(self, item: dict[str, Any], founder: str, start: datetime) -> EmailInteraction | None:
        occurred_at = _parse_datetime(item.get("date"))
        if occurred_at is None or occurred_at < start:
            return None
        direction = self._direction(item.get("from"), founder)
        if direction is None:
            return None
        subject = _str(item.get("subject"))
        return EmailInteraction(
            direction=direction,
            occurred_at=occurred_at,
            subject=subject[:_MAX_SUBJECT_LEN] if subject else None,
        )

    def _direction(self, sender: Any, founder: str) -> InteractionDirection | None:
        """Classify by who sent it. Affinity's integer ``direction`` is not trusted:
        it is relative to whichever user's mailbox synced the email."""
        addresses = _person_addresses(sender)
        if any(_domain_of(a) in self._dsw_domains for a in addresses):
            return InteractionDirection.OUTBOUND
        if founder in addresses:
            return InteractionDirection.INBOUND
        return None  # a third party (e.g. a CC'd co-investor) - irrelevant to tracking


def _field_items(value: dict[str, Any]) -> list[dict[str, Any]]:
    data = value.get("data")
    if isinstance(data, dict):  # single-valued field
        return [data]
    return _dicts(data)


def _full_name(first: Any, last: Any) -> str | None:
    parts = [p for p in (_str(first), _str(last)) if p]
    return " ".join(parts) or None
