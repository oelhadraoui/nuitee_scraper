"""Unit tests for src/utils/text.py helpers not covered elsewhere."""

from __future__ import annotations

import pytest

from src.utils.text import parse_distance_km


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3 km from centre", 3.0),
        ("225 km from centre", 225.0),
        ("1.5 km from the centre", 1.5),
        ("12,4 km from centre", 12.4),
        ("1,234 km from centre", 1234.0),
        ("850 m from center", 0.85),
        ("Rive Hotel\nAvenue d'alger\n3 km from centre\nBreakfast included", 3.0),
    ],
)
def test_parse_distance_km(text: str, expected: float) -> None:
    assert parse_distance_km(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "Free cancellation", "MAD 941 / night"])
def test_parse_distance_km_absent(text: str) -> None:
    assert parse_distance_km(text) is None
