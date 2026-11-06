"""
src/pipeline/shortlist.py
=============================================================================
The client deliverable: a ranked shortlist of hotels where Nuitee's true
payable price is significantly below Booking.com's for the same stay.

"Same stay" is what makes the comparison like-for-like: only CLEAN rows are
used (see quality.py), i.e. the same hotel (fuzzy-matched on Booking), the
same check-in/check-out, the same occupancy, and both sides' whole-stay
totals including taxes and fees, normalised to MAD.

A hotel is shortlisted when, across every clean window it was priced in, the
MEDIAN gap clears both thresholds - a single lucky window is not enough:

    median((booking - nuitee) / nuitee)          >= min_gap_pct
    median((booking - nuitee) / nights)  (MAD)   >= min_gap_mad_per_night

The per-night figure keeps 3-night and 10-night stays on a comparable basis;
whole-stay gaps per window are in the detail file.

Outputs (both ``utf-8-sig`` so Excel opens accents correctly):

    shortlist.csv         one row per hotel, ranked
    shortlist_detail.csv  every clean window behind each shortlisted hotel
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Optional

from src.corridors import corridor_for
from src.models import market_scope_for

__all__ = ["ShortlistEntry", "build_shortlist", "render_shortlist", "write_shortlist"]


@dataclass(slots=True)
class ShortlistEntry:
    """One shortlisted hotel, aggregated over its clean windows."""

    hotel_name: str
    city: str
    country: str
    priority: bool
    corridor_rank: int
    windows: list[dict[str, Any]] = field(default_factory=list)
    overall_rank: int = 0
    city_rank: int = 0

    # -- aggregates ---------------------------------------------------------

    @property
    def median_gap_pct(self) -> float:
        return median(w["price_difference_pct"] for w in self.windows)

    @property
    def median_gap_per_night(self) -> float:
        return median(w["price_difference"] / w["nights"] for w in self.windows)

    @property
    def best_window(self) -> dict[str, Any]:
        return max(self.windows, key=lambda w: w["price_difference_pct"])

    @property
    def nuitee_cheaper_count(self) -> int:
        return sum(1 for w in self.windows if w["price_difference"] > 0)

    def to_row(self) -> dict[str, Any]:
        best = self.best_window
        return {
            "Overall Rank": self.overall_rank,
            "City Rank": self.city_rank,
            "Priority Corridor": "Yes" if self.priority else "No",
            "Country": self.country,
            "City": self.city,
            "Hotel Name": self.hotel_name,
            "Booking Matched Name": best.get("booking_matched_name") or "",
            "Median Gap (%)": round(self.median_gap_pct, 1),
            "Median Gap per Night (MAD)": round(self.median_gap_per_night),
            "Best Window": _window_label(best),
            "Best Window Nuitee (MAD, whole stay)": round(best["nuitee_final_price"]),
            "Best Window Booking (MAD, whole stay)": round(best["booking_price"]),
            "Best Window Gap (MAD)": round(best["price_difference"]),
            "Best Window Gap (%)": round(best["price_difference_pct"], 1),
            "Windows Compared": len(self.windows),
            "Windows Nuitee Cheaper": self.nuitee_cheaper_count,
            "Min Fuzzy Score": round(min(w["fuzzy_score"] or 0 for w in self.windows)),
            "Nuitee Room (best window)": best.get("nuitee_room_label") or "",
            "Nuitee URL": best.get("nuitee_url") or "",
            "Booking URL": best.get("booking_url") or "",
        }


DETAIL_COLUMNS = [
    "Overall Rank", "Country", "City", "Hotel Name", "Offset", "Nights",
    "Check-in", "Check-out", "Nuitee Final Payable (MAD)", "Booking Price (MAD)",
    "Gap (MAD)", "Gap (%)", "Fuzzy Score", "Nuitee Room", "Nuitee URL",
    "Booking URL",
]


def build_shortlist(
    rows: list[dict[str, Any]],
    *,
    min_gap_pct: float = 5.0,
    min_gap_mad_per_night: float = 20.0,
    min_windows: int = 1,
    international_only: bool = False,
    sort: str = "corridor",
) -> list[ShortlistEntry]:
    """
    Aggregate clean store rows into a ranked shortlist.

    ``sort="corridor"`` (default) presents priority corridors first in the
    client's country/city order, best gap first within each city;
    ``sort="gap"`` is a single ranking by median gap %, priority first.
    ``overall_rank`` is always the pure median-gap-% ranking.
    """
    hotels: dict[tuple[str, str], ShortlistEntry] = {}
    for row in rows:
        if not _usable(row):
            continue
        city = row["city"]
        if international_only and market_scope_for(city) != "International":
            continue
        key = (row.get("hotel_name_normalised") or row["hotel_name"].lower(), city.lower())
        entry = hotels.get(key)
        if entry is None:
            corridor = corridor_for(city)
            if corridor:
                country = corridor.country
            else:
                country = "Morocco" if market_scope_for(city) != "International" else ""
            entry = hotels[key] = ShortlistEntry(
                hotel_name=row["hotel_name"],
                city=city,
                country=country,
                priority=corridor is not None,
                corridor_rank=corridor.rank if corridor else 10_000,
            )
        entry.windows.append(row)

    shortlisted = [
        e for e in hotels.values()
        if len(e.windows) >= min_windows
        and e.median_gap_pct >= min_gap_pct
        and e.median_gap_per_night >= min_gap_mad_per_night
    ]

    for rank, entry in enumerate(
        sorted(shortlisted, key=lambda e: -e.median_gap_pct), start=1
    ):
        entry.overall_rank = rank

    by_city: dict[str, list[ShortlistEntry]] = {}
    for entry in shortlisted:
        by_city.setdefault(entry.city.lower(), []).append(entry)
    for group in by_city.values():
        for rank, entry in enumerate(sorted(group, key=lambda e: e.overall_rank), start=1):
            entry.city_rank = rank

    if sort == "gap":
        key = lambda e: (not e.priority, e.overall_rank)  # noqa: E731
    else:
        key = lambda e: (e.corridor_rank, e.city.lower(), e.city_rank)  # noqa: E731
    return sorted(shortlisted, key=key)


def write_shortlist(
    entries: list[ShortlistEntry], path: Path, detail_path: Optional[Path] = None
) -> tuple[Path, Optional[Path]]:
    """Write the ranked shortlist (and optionally its per-window detail)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [e.to_row() for e in entries]
    header = list(rows[0]) if rows else list(_empty_row_header())
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)

    if detail_path is not None:
        with detail_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(DETAIL_COLUMNS)
            for entry in entries:
                for w in sorted(entry.windows, key=lambda w: (w["horizon_label"], w["nights"])):
                    writer.writerow([
                        entry.overall_rank, entry.country, entry.city, entry.hotel_name,
                        w["horizon_label"], w["nights"], w["checkin"], w["checkout"],
                        round(w["nuitee_final_price"], 2), round(w["booking_price"], 2),
                        round(w["price_difference"], 2), round(w["price_difference_pct"], 2),
                        w.get("fuzzy_score"), w.get("nuitee_room_label") or "",
                        w.get("nuitee_url") or "", w.get("booking_url") or "",
                    ])
    return path, detail_path


def render_shortlist(entries: list[ShortlistEntry], limit: int = 25) -> str:
    """Console preview of the top of the shortlist."""
    lines = [
        f"Shortlist: {len(entries)} hotel(s) "
        f"({sum(e.priority for e in entries)} in priority corridors)",
        "-" * 96,
        f"{'#':>4}  {'Country':<16}{'City':<16}{'Hotel':<34}{'Gap %':>8}{'MAD/night':>11}{'Win':>5}",
    ]
    for e in entries[:limit]:
        lines.append(
            f"{e.overall_rank:>4}  {e.country[:15]:<16}{e.city[:15]:<16}"
            f"{e.hotel_name[:33]:<34}{e.median_gap_pct:>7.1f}%"
            f"{e.median_gap_per_night:>11.0f}{len(e.windows):>5}"
        )
    if len(entries) > limit:
        lines.append(f"  ... and {len(entries) - limit} more in the CSV")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _usable(row: dict[str, Any]) -> bool:
    """A clean row with both prices and a positive stay length."""
    return (
        bool(row.get("is_clean"))
        and row.get("nuitee_final_price") is not None
        and row.get("booking_price") is not None
        and row.get("price_difference") is not None
        and row.get("price_difference_pct") is not None
        and int(row.get("nights") or 0) > 0
    )


def _window_label(window: dict[str, Any]) -> str:
    return f"{window['horizon_label']} / {window['nights']}n ({window['checkin']})"


def _empty_row_header() -> dict[str, Any]:
    stub = ShortlistEntry("", "", "", False, 0, windows=[{
        "price_difference_pct": 0.0, "price_difference": 0.0, "nights": 1,
        "nuitee_final_price": 0.0, "booking_price": 0.0, "fuzzy_score": 0,
        "horizon_label": "", "checkin": "",
    }])
    return stub.to_row()
