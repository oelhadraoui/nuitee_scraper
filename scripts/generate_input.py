#!/usr/bin/env python3
"""
scripts/generate_input.py
=============================================================================
Generate ``input.json`` for the horizon-driven pipeline.

The new input format lists CITIES ONLY. Check-in / check-out dates are no
longer stored in the file - they are derived at runtime from today's date
plus the configured offsets (a short ~1-week-out offset and a long
~3-months-out offset, each tested at a couple of stay durations). Hard-coding
dates in the input would defeat the point of always measuring "as of today".

    {
      "version": 2,
      "rooms_config": [2],
      "cities": ["Casablanca", "Marrakech", ...]
    }

The legacy array-of-objects format is still accepted by the CLI; any
``checkin``/``checkout`` keys in it are ignored.

Usage
-----
    python scripts/generate_input.py                       # default set
    python scripts/generate_input.py --preset morocco
    python scripts/generate_input.py --cities "Paris,Rome" --adults 2 --rooms 1
    python scripts/generate_input.py --preview-plan --durations 3,10
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

PRESETS: dict[str, list[str]] = {
    # Domestic inventory - densest coverage on a Morocco-facing white-label,
    # and therefore the fastest route to the 1,000-clean-hotel target.
    "morocco": [
        "Casablanca", "Marrakech", "Rabat", "Tangier", "Agadir", "Fes",
        "Essaouira", "Meknes", "Chefchaouen", "Ouarzazate", "Dakhla",
        "El Jadida", "Ifrane", "Oujda", "Tetouan", "Nador", "Saidia",
        "Merzouga", "Taghazout", "Asilah",
    ],
    # Short-haul markets with high Moroccan outbound demand.
    "europe": [
        "Paris", "London", "Madrid", "Barcelona", "Rome", "Milan", "Berlin",
        "Munich", "Amsterdam", "Brussels", "Lisbon", "Porto", "Vienna",
        "Prague", "Budapest", "Athens", "Istanbul", "Zurich", "Stockholm",
        "Copenhagen", "Marseille", "Lyon", "Seville", "Malaga",
    ],
    "mena": [
        "Dubai", "Doha", "Riyadh", "Abu Dhabi", "Cairo", "Tunis", "Algiers",
        "Amman", "Beirut", "Muscat", "Jeddah", "Kuwait City",
    ],
    "world": [
        "New York", "Los Angeles", "Chicago", "Miami", "Toronto", "Vancouver",
        "Mexico City", "Sao Paulo", "Rio de Janeiro", "Buenos Aires", "Lima",
        "Santiago", "Dakar", "Lagos", "Nairobi", "Cape Town", "Johannesburg",
        "Bangkok", "Singapore", "Kuala Lumpur", "Tokyo", "Seoul", "Sydney",
        "Melbourne",
    ],
}

#: Default: domestic first (best inventory), then Europe, then the rest.
DEFAULT_PRESETS = ["morocco", "europe", "mena", "world"]


def build_city_list(presets: list[str], extra: list[str]) -> list[str]:
    """Merge presets in order, de-duplicated, preserving priority order."""
    cities: list[str] = []
    seen: set[str] = set()
    for preset in presets:
        for city in PRESETS.get(preset, []):
            if city.lower() not in seen:
                seen.add(city.lower())
                cities.append(city)
    for city in extra:
        city = city.strip()
        if city and city.lower() not in seen:
            seen.add(city.lower())
            cities.append(city)
    return cities


def estimate_volume(
    n_cities: int, n_durations: int, hotels_per_city: int, n_offsets: int = 2
) -> dict[str, int]:
    """
    Rough volume projection, so you can size the run before starting it.

    Every city produces ``n_offsets x n_durations`` search windows (offsets
    from today, e.g. short ~1 week and long ~3 months; durations, e.g. 3 and
    10 nights) - not a sweep of simulated scraping dates, so the count stays
    small and predictable regardless of how wide the offsets are.
    """
    windows = n_cities * n_offsets * n_durations
    rows = windows * hotels_per_city
    return {
        "windows": windows,
        "max_rows": rows,
        # ~45% of harvested hotels typically survive the clean gate once
        # tunnel failures and Booking match failures are accounted for.
        "expected_clean_rows": int(rows * 0.45),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate input.json")
    parser.add_argument(
        "--output", "-o", type=Path, default=Path("input.json")
    )
    parser.add_argument(
        "--preset", action="append", choices=[*list(PRESETS), "all"], default=None,
        help="City preset (repeatable). Default: all, domestic first.",
    )
    parser.add_argument("--cities", default="", help="Extra comma-separated cities")
    parser.add_argument("--adults", type=int, default=2, help="Adults per room")
    parser.add_argument("--rooms", type=int, default=1, help="Rooms per booking")
    parser.add_argument(
        "--legacy-format", action="store_true",
        help="Emit the old array-of-objects shape (with placeholder dates)",
    )
    parser.add_argument(
        "--preview-plan", action="store_true",
        help="Print a volume projection instead of just writing the file",
    )
    parser.add_argument(
        "--durations", default="3,10",
        help="Comma-separated stay lengths in nights, for the volume preview only",
    )
    parser.add_argument("--hotels-per-city", type=int, default=25)
    args = parser.parse_args()

    presets = args.preset or DEFAULT_PRESETS
    if "all" in presets:
        presets = list(PRESETS)

    extra = [c for c in args.cities.split(",") if c.strip()]
    cities = build_city_list(presets, extra)
    rooms_config = [args.adults] * args.rooms

    if args.legacy_format:
        today = date.today().isoformat()
        payload = [
            {
                "city": city,
                "checkin": today,      # ignored at runtime - horizons win
                "checkout": today,
                "adults": args.adults,
                "rooms": args.rooms,
            }
            for city in cities
        ]
    else:
        payload = {
            "version": 2,
            "generated_at": date.today().isoformat(),
            "note": (
                "Dates are derived at runtime from today's date plus the "
                "configured offsets (short ~1 week, long ~3 months). "
                "Do not hard-code them here."
            ),
            "rooms_config": rooms_config,
            "cities": cities,
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {len(cities)} cities to {args.output}")

    if args.preview_plan:
        durations = [int(d.strip()) for d in args.durations.split(",") if d.strip()]
        estimate = estimate_volume(len(cities), len(durations), args.hotels_per_city)
        print("\nVolume projection")
        print("-" * 48)
        print(f"  cities              : {len(cities)}")
        print("  offsets             : 2 (short ~1 week, long ~3 months)")
        print(f"  stay durations      : {durations}")
        print(f"  search windows      : {estimate['windows']:,}")
        print(f"  max rows            : {estimate['max_rows']:,}")
        print(f"  expected clean rows : ~{estimate['expected_clean_rows']:,}")
        print("-" * 48)
        if estimate["expected_clean_rows"] < 1_000:
            print("  WARNING: projection is below the 1,000 clean-hotel target.")
            print("  Increase --hotels-per-city or add more cities.\n")
        else:
            print("  Projection clears the 1,000 clean-hotel target.\n")


if __name__ == "__main__":
    main()
