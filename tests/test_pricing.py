"""Tests for price parsing and currency normalisation."""

from __future__ import annotations

import pytest

from src.utils.pricing import (
    detect_currency,
    drop_addon_amounts,
    extract_amounts,
    format_mad,
    get_fx_rate,
    parse_price,
    surcharge_amount,
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


class TestDateNoiseStripping:
    """
    A checkout page's own check-in/check-out date line ("Aug 4, 2026") must
    never be mistaken for a price. This is not hypothetical: a live sweep of
    Nuitee's booking page (Puerta Azul / Chefchaouen, 2026-08-04) found the
    bare year "2026" was the single largest number on the page - bigger than
    the real MAD 1,899.36 total - which is exactly the failure mode a
    strategy="max" fallback sweep is vulnerable to.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Check-in & Check-out Aug 4, 2026 - Aug 7, 2026 (3 nights)",
            "Cancel by Aug 2, 2026 07:59 PM",
            "4 Aug 2026",
            "Aug 4-7, 2026",
        ],
    )
    def test_bare_calendar_dates_yield_no_price(self, text: str) -> None:
        assert parse_price(text, min_value=150.0).amount_mad is None

    def test_year_does_not_beat_the_real_total_in_a_wide_sweep(self) -> None:
        """
        Regression test for the exact live scenario: a full-page sweep whose
        text includes the check-in/check-out dates, a cancellation-policy
        panel, and the real grand total. Strategy "max" must land on the
        total (1,899.36), not the year (2026) or a cancellation sub-amount.
        """
        body_text = (
            "Chefchaouen Aug 04 - Aug 07 2 Guests Puerta Azul "
            "Av. Maghreb arabe, Chefchaouene, MA "
            "9.4 Wonderful Based on 1,854 reviews "
            "Aug 4, 2026 - Aug 7, 2026 (3 nights) "
            "Cancel by Aug 2, 2026 07:59 PM Free Cancellation "
            "Full refund - MAD 1,754.11 "
            "Cancel after Aug 2, 2026 08:00 PM "
            "Cancellation charge MAD 584.70 Refund MAD 1,169.41 "
            "MAD 584.70 average per night 1 room x 3 nights MAD 1,754.11 "
            "Local fees MAD 145.25 "
            "Total MAD 1,899.36 Includes taxes and fees "
            "Pay now MAD 1,754.11 Pay at Property MAD 145.25"
        )
        parsed = parse_price(
            body_text, min_value=150.0, max_value=200_000.0, strategy="max"
        )
        assert parsed.amount_mad == pytest.approx(1899.36)

    def test_price_equal_to_a_plausible_year_is_the_only_blind_spot(self) -> None:
        """
        Documented trade-off: a genuine price that happens to equal a
        four-digit year immediately preceded by a month name would also be
        stripped. This is an acceptable, extremely rare false negative next
        to the alternative (years routinely masquerading as prices).
        """
        assert extract_amounts("Aug 4, 2026", min_value=150.0) == []


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


class TestAddonAmounts:
    """A "+<amount>" surcharge on a Nuitee card must never win as the rate."""

    def _card(self, text: str):
        return parse_price(drop_addon_amounts(text), strategy="min")

    @pytest.mark.parametrize(
        "text,expected",
        [
            # Live: Vincci Larios Diez - the tunnel clicked 'Offer #14 @ 239.8'.
            ("MAD 2,136.40 +MAD 239.80 taxes and fees", 2136.40),
            ("Room with Breakfast\nMAD 2,147\n(+MAD 250.70 taxes and fees)", 2147.0),
            # Struck-through original still loses to the discounted rate.
            ("MAD 701 MAD 591 +MAD 60 taxes and fees", 591.0),
            ("MAD 1,299 + MAD 163.50 taxes and fees", 1299.0),
            ("€ 196 + € 22 taxes and fees", 196 * 10.90),
            ("MAD 980 +414.20 local fees", 980.0),
        ],
    )
    def test_rate_wins_over_city_tax(self, text: str, expected: float) -> None:
        assert self._card(text).amount_mad == pytest.approx(expected)

    def test_plain_rate_untouched(self) -> None:
        assert drop_addon_amounts("MAD 1,450") == "MAD 1,450"
        assert self._card("MAD 1,450 per night").amount_mad == 1450.0

    def test_only_a_surcharge_gives_no_price(self) -> None:
        assert self._card("+MAD 239.80 taxes and fees").ok is False

    def test_empty(self) -> None:
        assert drop_addon_amounts(None) == ""


class TestBookingTaxesLine:
    """Booking's card taxes line, as read live from Morocco and from France."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("+MAD 263 taxes and fees", 263.0),
            ("+MAD 2,154 taxes", 2154.0),
            ("+ MAD 1,368.50 taxes and charges", 1368.5),
            ("+€ 24 taxes and charges", 24 * 10.90),
        ],
    )
    def test_separate_taxes_are_read(self, text: str, expected: float) -> None:
        assert surcharge_amount(text) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "text", ["Includes taxes and fees", "Includes taxes and charges", "", None]
    )
    def test_included_or_missing_adds_nothing(self, text) -> None:
        assert surcharge_amount(text) is None
