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

__all__ = ["CORRIDORS_PATH", "CityCorridor", "corridor_for"]

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

