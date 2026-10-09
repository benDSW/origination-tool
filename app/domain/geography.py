"""UK / London / inside-M25 place recognition.

Evertrace's own ``city=!London`` filter only removes the literal city "London",
so towns and boroughs inside the M25 slip through. This module is the tool's
second line of defence. The list is deliberately conservative (it errs towards
treating border towns as inside the M25) and easy to extend.
"""

from __future__ import annotations

import re

UK_COUNTRY_NAMES: frozenset[str] = frozenset(
    {
        "united kingdom",
        "uk",
        "u.k.",
        "great britain",
        "gb",
        "england",
        "scotland",
        "wales",
        "northern ireland",
    }
)

# The 32 London boroughs + City of London, plus common district names that
# appear as a LinkedIn "city", plus towns inside (or straddling) the M25.
INSIDE_M25_PLACES: frozenset[str] = frozenset(
    {
        # London itself
        "london",
        "greater london",
        "city of london",
        "central london",
        "east london",
        "west london",
        "north london",
        "south london",
        "london area",
        "greater london area",
        # Boroughs
        "barking",
        "barking and dagenham",
        "dagenham",
        "barnet",
        "bexley",
        "bexleyheath",
        "brent",
        "bromley",
        "camden",
        "croydon",
        "ealing",
        "enfield",
        "greenwich",
        "hackney",
        "hammersmith",
        "hammersmith and fulham",
        "fulham",
        "haringey",
        "harrow",
        "havering",
        "hillingdon",
        "hounslow",
        "islington",
        "kensington",
        "kensington and chelsea",
        "chelsea",
        "kingston",
        "kingston upon thames",
        "lambeth",
        "lewisham",
        "merton",
        "newham",
        "redbridge",
        "richmond",
        "richmond upon thames",
        "southwark",
        "sutton",
        "tower hamlets",
        "waltham forest",
        "wandsworth",
        "westminster",
        # Districts that often appear as a city
        "shoreditch",
        "canary wharf",
        "stratford",
        "wimbledon",
        "clapham",
        "brixton",
        "hampstead",
        "walthamstow",
        "ilford",
        "romford",
        "wembley",
        "uxbridge",
        "twickenham",
        "teddington",
        "orpington",
        "sidcup",
        "woolwich",
        "putney",
        "battersea",
        "chiswick",
        "acton",
        "southall",
        "edgware",
        "finchley",
        "tottenham",
        "wood green",
        "kingston-upon-thames",
        "richmond-upon-thames",
        # Towns inside or straddling the M25 (outside Greater London)
        "watford",
        "borehamwood",
        "bushey",
        "potters bar",
        "rickmansworth",
        "epsom",
        "ewell",
        "esher",
        "weybridge",
        "walton-on-thames",
        "walton on thames",
        "staines",
        "staines-upon-thames",
        "sunbury-on-thames",
        "sunbury",
        "leatherhead",
        "cheshunt",
        "waltham cross",
        "loughton",
        "chigwell",
        "dartford",
        "swanley",
        "caterham",
        "banstead",
        "radlett",
        "elstree",
    }
)

_SUFFIXES = re.compile(r",?\s*(england|united kingdom|uk|great britain|gb)\s*$")
_SPACES = re.compile(r"\s+")


def normalise_place(value: str | None) -> str:
    if not value:
        return ""
    text = _SPACES.sub(" ", value.strip().lower())
    # Strip trailing country qualifiers repeatedly ("Croydon, England, United Kingdom").
    while True:
        stripped = _SUFFIXES.sub("", text).strip(" ,")
        if stripped == text:
            break
        text = stripped
    return text


def is_uk(country: str | None) -> bool:
    return normalise_place(country) in UK_COUNTRY_NAMES or (country or "").strip().lower() in UK_COUNTRY_NAMES


def is_inside_m25(city: str | None) -> bool:
    place = normalise_place(city)
    if not place:
        return False
    if place in INSIDE_M25_PLACES:
        return True
    # "London Borough of X", "X, London", "X, Greater London"
    parts = [p.strip() for p in place.split(",")]
    if any(p in INSIDE_M25_PLACES for p in parts):
        return True
    return place.startswith("london borough of ")
