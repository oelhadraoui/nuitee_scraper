"""
src/corridors.py
=============================================================================
The client's priority corridors (``config/corridors.json``): which cities are
scraped first, which country each belongs to, and in what order the
shortlist presents them.

The JSON file is shared with ``scripts/generate_input.py`` (which must stay
a zero-dependency standalone script), so the list lives in exactly one place
instead of being mirrored the way ``MOROCCO_CITIES`` is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from src.models import MOROCCO_CITIES

__all__ = [
    "CORRIDORS_PATH",
    "CityCorridor",
    "corridor_for",
    "country_for",
    "country_code_for",
    "country_labels",
    "nuitee_place_name",
]

CORRIDORS_PATH = Path(__file__).resolve().parent.parent / "config" / "corridors.json"


@dataclass(frozen=True, slots=True)
class CityCorridor:
    """Where a priority city sits in the client's brief."""

    country: str
    rank: int  # 0-based position of the city in the full priority list


@lru_cache(maxsize=4)
def _load(path: Path) -> dict[str, CityCorridor]:
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[str, CityCorridor] = {}
    for group in data.get("priority", []):
        for city in group.get("cities", []):
            key = city.strip().lower()
            if key and key not in mapping:
                mapping[key] = CityCorridor(group["country"], len(mapping))
    return mapping


def corridor_for(city: str, path: Path = CORRIDORS_PATH) -> Optional[CityCorridor]:
    """The city's priority corridor, or ``None`` for a non-priority city."""
    return _load(path).get((city or "").strip().lower())



#: ISO code (as in Booking.com hotel URLs, ``/hotel/<code>/``) and the
#: spellings Nuitee's autocomplete uses for each country - live-read
#: 2026-11: "London / UK", "New York / NY, USA", "Bodrum / Mugla, Türkiye".
_COUNTRIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "Morocco": ("ma", ("Morocco",)),
    "France": ("fr", ("France",)),
    "Spain": ("es", ("Spain",)),
    "Portugal": ("pt", ("Portugal",)),
    "Italy": ("it", ("Italy",)),
    "Switzerland": ("ch", ("Switzerland",)),
    "Germany": ("de", ("Germany",)),
    "Netherlands": ("nl", ("Netherlands",)),
    "Belgium": ("be", ("Belgium",)),
    "United Kingdom": ("gb", ("United Kingdom", "UK")),
    "Turkey": ("tr", ("Turkey", "Türkiye")),
    "United Arab Emirates": ("ae", ("United Arab Emirates", "UAE")),
    "Saudi Arabia": ("sa", ("Saudi Arabia",)),
    "Thailand": ("th", ("Thailand",)),
    "Vietnam": ("vn", ("Vietnam", "Viet Nam")),
    "China": ("cn", ("China",)),
    "Malaysia": ("my", ("Malaysia",)),
    "United States": ("us", ("United States", "USA")),
    "Canada": ("ca", ("Canada",)),
    "Mexico": ("mx", ("Mexico",)),
}


def country_for(city: str, path: Path = CORRIDORS_PATH) -> Optional[str]:
    """The city's country, or ``None`` when the city is in neither list."""
    if (city or "").strip().lower() in MOROCCO_CITIES:
        return "Morocco"
    corridor = corridor_for(city, path)
    return corridor.country if corridor else None


def country_code_for(city: str, path: Path = CORRIDORS_PATH) -> Optional[str]:
    """Lower-case ISO code of the city's country, or ``None`` if unknown."""
    info = _COUNTRIES.get(country_for(city, path) or "")
    return info[0] if info else None


def country_labels(city: str, path: Path = CORRIDORS_PATH) -> tuple[str, ...]:
    """How Nuitee's autocomplete may write the city's country (empty if unknown)."""
    info = _COUNTRIES.get(country_for(city, path) or "")
    return info[1] if info else ()


#: Cities Nuitee's autocomplete only lists under another name - live-read
#: 2026-11: typing "Brussels" offers the airport and hotels, never the city.
_NUITEE_PLACE_NAMES: dict[str, str] = {
    "brussels": "Bruxelles",
    "frankfurt": "Frankfurt am Main",
    "langkawi": "Langkawi Island",
}


def nuitee_place_name(city: str) -> str:
    """What to type into Nuitee's destination box for ``city``."""
    return _NUITEE_PLACE_NAMES.get((city or "").strip().lower(), city)
