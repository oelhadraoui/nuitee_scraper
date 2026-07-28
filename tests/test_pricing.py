"""Tests for price parsing and currency normalisation."""

from __future__ import annotations

import pytest

from src.utils.pricing import (
    detect_currency,
    extract_amounts,
    format_mad,
    get_fx_rate,
    parse_price,
    to_mad,
)


class TestNoiseStripping:
    """The parser must never mistake review counts or distances for a price."""

    @pytest.mark.parametrize(
        "text",
        [
            "8.7 Excellent 144 reviews",
            "Scored 9.1 - 1,204 reviews",
            "0.3 km from centre",
            "2 miles from the beach",
            "Check-in 14:00",
            "2 adults, 1 room, 1 night",
        ],
    )
    def test_pure_noise_yields_no_price(self, text: str) -> None:
        assert parse_price(text).amount_mad is None

    def test_price_survives_surrounding_noise(self) -> None:
        text = "Hotel Anfa 8.7 Excellent 1,204 reviews 0.3 km MAD 1,250 per night"
        assert parse_price(text).amount_mad == 1250.0


class TestSeparators:
    """Locale-specific number formatting must all normalise identically."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("MAD 1,250.50", 1250.50),      # en: comma thousands, dot decimal
            ("1 234,56 MAD", 1234.56),      # fr: space thousands, comma decimal
            ("1.234,56 MAD", 1234.56),      # de: dot thousands, comma decimal
            ("1'234 MAD", 1234.0),          # ch: apostrophe thousands
            ("1\xa0234 MAD", 1234.0),       # nbsp thousands
            ("Total: 1 234 MAD", 1234.0),   # ascii space + currency suffix
            ("980 MAD", 980.0),             # plain
        ],
    )
    def test_separator_normalisation(self, text: str, expected: float) -> None:
        assert parse_price(text).amount_mad == pytest.approx(expected)

    def test_ascii_space_does_not_merge_unrelated_numbers(self) -> None:
        """'890 1250' must not become 8901250 - the classic silent-garbage bug."""
        amounts = extract_amounts("890 1250", min_value=100)
        assert 8901250.0 not in amounts
        assert 890.0 in amounts and 1250.0 in amounts


class TestStrategies:
    def test_max_picks_the_total(self) -> None:
        text = "Room 800 MAD + taxes 200 MAD = total 1000 MAD"
        assert parse_price(text, strategy="max").amount_mad == 1000.0

    def test_min_picks_the_discounted_price(self) -> None:
        """Booking shows a struck-through anchor next to the payable price."""
        text = "MAD 2,400 MAD 1,890"
        assert parse_price(text, strategy="min").amount_mad == 1890.0

    def test_first_picks_the_headline(self) -> None:
        text = "Total 1500 MAD, of which taxes 300 MAD"
        assert parse_price(text, strategy="first").amount_mad == 1500.0


class TestCurrency:
    @pytest.mark.parametrize(
        "text,code",
        [
            ("1250 MAD", "MAD"),
            ("95 EUR", "EUR"),
            ("110 €", "EUR"),
            ("$120", "USD"),
            ("£90", "GBP"),
            ("1250 DH", "MAD"),
        ],
    )
    def test_detection(self, text: str, code: str) -> None:
        assert detect_currency(text) == code

    def test_conversion_to_mad(self) -> None:
        parsed = parse_price("95,00 € total")
        assert parsed.currency == "EUR"
        assert parsed.converted is True
        assert parsed.raw_amount == 95.0
        assert parsed.amount_mad == pytest.approx(95.0 * get_fx_rate("EUR"), rel=1e-6)

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FX_RATE_EUR", "11.50")
        assert get_fx_rate("EUR") == 11.50

    def test_unknown_currency_is_not_faked(self) -> None:
        assert to_mad(100.0, "XYZ") is None

    def test_low_source_currency_price_is_not_rejected(self) -> None:
        """95 EUR (~1,035 MAD) must clear a 150 MAD floor after conversion."""
        assert parse_price("95 EUR", min_value=150.0).amount_mad is not None


class TestBounds:
    def test_below_floor_rejected(self) -> None:
        assert parse_price("MAD 80", min_value=150.0).amount_mad is None

    def test_above_ceiling_rejected(self) -> None:
        assert parse_price("MAD 999999", max_value=200_000.0).amount_mad is None

    def test_empty_input(self) -> None:
        assert parse_price("").amount_mad is None
        assert format_mad(None) == ""
        assert format_mad(1250.5) == "1250.50"
