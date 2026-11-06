"""Tests for the Nuitee-cheaper hotel shortlist (the client deliverable)."""

from __future__ import annotations

import csv
from pathlib import Path

from src.corridors import corridor_for
from src.pipeline.shortlist import build_shortlist, write_shortlist


def _row(hotel: str, city: str, nuitee: float, booking: float, *,
         nights: int = 3, label: str = "1W", clean: bool = True) -> dict:
    diff = booking - nuitee
    return {
        "hotel_name": hotel,
        "hotel_name_normalised": hotel.lower(),
        "city": city,
        "checkin": "2026-11-01",
        "checkout": "2026-11-04",
        "nights": nights,
        "horizon_label": label,
        "nuitee_final_price": nuitee,
        "booking_price": booking,
        "price_difference": diff,
        "price_difference_pct": diff / nuitee * 100,
        "fuzzy_score": 95.0,
        "is_clean": 1 if clean else 0,
        "booking_matched_name": hotel,
    }


class TestCorridors:
    def test_lookup_is_case_insensitive(self) -> None:
        corridor = corridor_for("  paris ")
        assert corridor is not None and corridor.country == "France"

    def test_order_follows_the_brief(self) -> None:
        assert corridor_for("Paris").rank < corridor_for("Madrid").rank < corridor_for("Dubai").rank

    def test_non_priority_city(self) -> None:
        assert corridor_for("Casablanca") is None


class TestBuildShortlist:
    def test_keeps_only_significant_gaps(self) -> None:
        rows = [
            _row("Cheap Inn", "Paris", 1000, 1200),     # 20%, 66 MAD/night
            _row("Tiny Gap", "Paris", 1000, 1020),      # 2%
            _row("Booking Wins", "Paris", 1200, 1000),
        ]
        names = [e.hotel_name for e in build_shortlist(rows)]
        assert names == ["Cheap Inn"]

    def test_mad_floor_per_night(self) -> None:
        # 10% but only 10 MAD/night on a cheap 3-night stay.
        rows = [_row("Budget", "Paris", 300, 330)]
        assert build_shortlist(rows) == []
        assert len(build_shortlist(rows, min_gap_mad_per_night=5)) == 1

    def test_median_across_windows_not_best_window(self) -> None:
        rows = [
            _row("Lucky Once", "Rome", 1000, 1500, label="1W"),
            _row("Lucky Once", "Rome", 1000, 990, label="3M"),
            _row("Lucky Once", "Rome", 1000, 980, label="3M", nights=10),
        ]
        assert build_shortlist(rows) == []

    def test_unclean_rows_ignored(self) -> None:
        assert build_shortlist([_row("X", "Paris", 1000, 1500, clean=False)]) == []

    def test_corridor_sort_and_ranks(self) -> None:
        rows = [
            _row("Fes Riad", "Fes", 1000, 1600),       # biggest gap, not priority
            _row("Dubai Tower", "Dubai", 1000, 1300),
            _row("Paris A", "Paris", 1000, 1150),
            _row("Paris B", "Paris", 1000, 1250),
        ]
        entries = build_shortlist(rows)
        assert [e.hotel_name for e in entries] == ["Paris B", "Paris A", "Dubai Tower", "Fes Riad"]
        assert [e.city_rank for e in entries] == [1, 2, 1, 1]
        assert entries[-1].overall_rank == 1
        assert entries[0].country == "France" and entries[-1].country == "Morocco"

    def test_gap_sort_puts_priority_first(self) -> None:
        rows = [_row("Fes Riad", "Fes", 1000, 1600), _row("Paris A", "Paris", 1000, 1150)]
        entries = build_shortlist(rows, sort="gap")
        assert [e.hotel_name for e in entries] == ["Paris A", "Fes Riad"]

    def test_international_only(self) -> None:
        rows = [_row("Fes Riad", "Fes", 1000, 1600)]
        assert build_shortlist(rows, international_only=True) == []


class TestWriteShortlist:
    def test_writes_ranked_csv_and_detail(self, tmp_path: Path) -> None:
        rows = [
            _row("Paris A", "Paris", 1000, 1300, label="1W"),
            _row("Paris A", "Paris", 3000, 3600, label="3M", nights=10),
        ]
        entries = build_shortlist(rows)
        path, detail = write_shortlist(
            entries, tmp_path / "shortlist.csv", tmp_path / "shortlist_detail.csv"
        )
        with path.open(encoding="utf-8-sig") as handle:
            (record,) = list(csv.DictReader(handle))
        assert record["Country"] == "France"
        assert record["Windows Compared"] == "2"
        assert float(record["Median Gap (%)"]) == 25.0
        assert float(record["Best Window Gap (MAD)"]) == 300
        with detail.open(encoding="utf-8-sig") as handle:
            assert len(list(csv.DictReader(handle))) == 2

    def test_empty_shortlist_still_has_header(self, tmp_path: Path) -> None:
        path, _ = write_shortlist([], tmp_path / "shortlist.csv")
        header = path.read_text(encoding="utf-8-sig").splitlines()[0]
        assert "Median Gap (%)" in header
