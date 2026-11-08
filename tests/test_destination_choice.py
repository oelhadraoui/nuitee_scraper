"""
Tests for choosing the Nuitee destination suggestion and the city -> country
lookup behind it. The suggestion lists are real autocomplete rows read off
Nuitee in 2026-11, each one a city the old shortest-label rule got wrong.
"""

from __future__ import annotations

import pytest

from src.corridors import country_code_for, country_for, country_labels
from src.scrapers.booking import _card_name
from src.scrapers.nuitee import pick_city_suggestion

SUGGESTIONS = {
    "Turin": [
        "Turin\nMetropolitan City of Turin, Italy",
        "Turin Airport (TRN)\nMetropolitan City of Turin, Italy",
        "Turíngia\nGermany",
        "NH Torino Centro\nCorso Vittorio Emanuele II 104, Turin",
    ],
    "Faro": [
        "Lanzarote Island\nCanary Islands, Spain",
        "Faroe Islands",
        "Faro\nPortugal",
        "Faro International Airport (FAO)\nFaro, Portugal",
    ],
    "Venice": [
        "Venice\nMetropolitan City of Venice, Italy",
        "Venice Marco Polo Airport (VCE)\nMetropolitan City of Venice, Italy",
        "Venice Beach\nCA, USA",
    ],
    "Los Angeles": [
        "Los Ángeles\nChile",
        "Los Angeles\nCA, USA",
    ],
    "Montreal": [
        "Montréal\nFrance",
        "Montreal\nQC, Canada",
    ],
    "Guangzhou": [
        "Guangzhou\nGuangdong Province, China",
        "Guangzhou Baiyun International Airport (CAN)\nGuangdong Province, China",
        "Guangzhou\nPrivadas Turquesa, Cancun, QROO, 77500, Cancun",
    ],
    "Cancun": [
        "Cancun\nPASEO NEPTUNO,65, Gandia",
        "Cancun\n5 Daniel, Netanya",
        "Cancún\nQuintana Roo, Mexico",
    ],
    "Bodrum": [
        "Bodrum\nMuğla, Türkiye",
        "Milas–Bodrum Airport (BJV)\nMuğla, Türkiye",
    ],
    "London": ["London\nUK", "London Luton Airport (LTN)\nLuton, UK"],
    "Tangier": ["Tangier\nMorocco", "Tangier Island\nVirginia, USA"],
}

EXPECTED = {
    "Turin": 0,
    "Faro": 2,
    "Venice": 0,
    "Los Angeles": 1,
    "Montreal": 1,
    "Guangzhou": 0,
    "Cancun": 2,
    "Bodrum": 0,
    "London": 0,
    "Tangier": 0,
}


class TestPickCitySuggestion:
    @pytest.mark.parametrize("city", sorted(SUGGESTIONS))
    def test_picks_the_city_in_its_country(self, city: str) -> None:
        index = pick_city_suggestion(SUGGESTIONS[city], city, country_labels(city))
        assert index == EXPECTED[city]

    def test_no_exact_place_means_no_pick(self) -> None:
        # Saudi Medina is "Madinah" on Nuitee; "Medina" only lists Spain.
        texts = ["Medina del Campo\nSpain", "Medinaceli\nSpain"]
        assert pick_city_suggestion(texts, "Medina") is None

    def test_wrong_country_only_means_no_pick(self) -> None:
        texts = ["Rome\nGeorgia, USA", "Romania"]
        assert pick_city_suggestion(texts, "Rome", country_labels("Rome")) is None

    def test_unknown_country_takes_first_exact_place(self) -> None:
        texts = ["Vienna Airport\nAustria", "Vienna\nAustria", "Vienna\nVA, USA"]
        assert pick_city_suggestion(texts, "Vienna") == 1

    def test_empty_or_placeholder_list(self) -> None:
        assert pick_city_suggestion([], "Paris") is None
        assert pick_city_suggestion(["Near me\nNo Results Found"], "Paris") is None


class TestCountryLookup:
    def test_morocco_and_priority_cities(self) -> None:
        assert country_for("Tangier") == "Morocco"
        assert country_code_for(" imlil ") == "ma"
        assert country_for("Madinah") == "Saudi Arabia"
        assert country_code_for("London") == "gb"
        assert "Türkiye" in country_labels("Goreme")

    def test_unknown_city(self) -> None:
        assert country_for("Vienna") is None
        assert country_code_for("Vienna") is None
        assert country_labels("Vienna") == ()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hotel Majestic Opens in new window", "Hotel Majestic"),
        ("Hotel Majestic  opens in a new window ", "Hotel Majestic"),
        ("Hotel Majestic", "Hotel Majestic"),
    ],
)
def test_booking_card_name_drops_new_window_label(raw: str, expected: str) -> None:
    assert _card_name(raw) == expected
