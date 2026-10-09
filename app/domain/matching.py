"""Matching founders against what DSW has already seen in Affinity.

Pure functions: the Affinity adapter supplies a :class:`KnownEntities` snapshot;
services call :func:`find_crm_match` per founder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.domain.models import CompanyRef, CrmMatch, KnownEntities

_LEGAL_SUFFIXES = (
    "limited",
    "ltd",
    "plc",
    "llp",
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "gmbh",
    "uk",
)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalise_company_name(name: str | None) -> str:
    """'Lacurve Limited' -> 'lacurve'; 'The Acme Co. (UK) Ltd' -> 'acme'."""
    if not name:
        return ""
    text = name.lower().replace("&", " and ")
    text = _NON_ALNUM.sub(" ", text)
    words = _SPACES.sub(" ", text).strip().split(" ")
    if words and words[0] == "the":
        words = words[1:]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words = words[:-1]
    return " ".join(words)


def normalise_domain(value: str | None) -> str:
    """'https://www.Acme.co.uk/about' -> 'acme.co.uk'."""
    if not value:
        return ""
    text = value.strip().lower()
    if "://" not in text:
        text = "//" + text
    host = urlsplit(text).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host.strip(".")


def normalise_person_name(name: str | None) -> str:
    if not name:
        return ""
    text = _NON_ALNUM.sub(" ", name.lower())
    return _SPACES.sub(" ", text).strip()


@dataclass(frozen=True)
class _IndexEntry:
    list_name: str
    opportunity_name: str


@dataclass
class KnownIndex:
    """Lookup tables built once per run from a :class:`KnownEntities` snapshot."""

    by_company: dict[str, _IndexEntry] = field(default_factory=dict)
    by_domain: dict[str, _IndexEntry] = field(default_factory=dict)
    by_person: dict[str, _IndexEntry] = field(default_factory=dict)

    @classmethod
    def build(cls, known: KnownEntities) -> KnownIndex:
        index = cls()
        for entity in known.entities:
            entry = _IndexEntry(entity.list_name, entity.opportunity_name)
            for company in (*entity.company_names, entity.opportunity_name):
                key = normalise_company_name(company)
                if key:
                    index.by_company.setdefault(key, entry)
            for domain in entity.company_domains:
                key = normalise_domain(domain)
                if key:
                    index.by_domain.setdefault(key, entry)
            for person in entity.person_names:
                key = normalise_person_name(person)
                # Single-token names are too ambiguous to exclude on.
                if key and " " in key:
                    index.by_person.setdefault(key, entry)
        return index


def find_crm_match(index: KnownIndex, company: CompanyRef | None, founder_name: str) -> CrmMatch | None:
    """Return why this founder is already known, or ``None`` if they are new.

    Order of precedence: website domain, company name, founder full name.
    """
    if company is not None:
        domain = normalise_domain(company.website_domain)
        if domain and domain in index.by_domain:
            e = index.by_domain[domain]
            return CrmMatch(
                matched_on="company_domain", list_name=e.list_name, opportunity_name=e.opportunity_name
            )
        name = normalise_company_name(company.name)
        if name and name in index.by_company:
            e = index.by_company[name]
            return CrmMatch(
                matched_on="company_name", list_name=e.list_name, opportunity_name=e.opportunity_name
            )
    person = normalise_person_name(founder_name)
    if person and person in index.by_person:
        e = index.by_person[person]
        return CrmMatch(matched_on="person_name", list_name=e.list_name, opportunity_name=e.opportunity_name)
    return None
