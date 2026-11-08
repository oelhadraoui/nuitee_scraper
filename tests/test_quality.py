"""Tests for the clean-row definition (Requirement B) and text matching."""

from __future__ import annotations

import pytest

from src.config import Settings
from src.models import MatchStatus, PriceRecord, TunnelStatus
from src.pipeline.quality import QualityFlag, QualityGate
from src.utils.text import fuzzy_score, normalise_key, strip_accents


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def gate(settings: Settings) -> QualityGate:
    return QualityGate(settings)


def make_record(**overrides) -> PriceRecord:
    """A record that is clean by default; override one field to dirty it."""
    defaults = dict(
        hotel_name="Hotel Barcelo Anfa",
        hotel_name_normalised="barcelo anfa",
        city="Casablanca",
        checkin="2026-07-30",
        checkout="2026-07-31",
        horizon_label="J+3",
        horizon_offset_days=3,
        execution_date="2026-07-27",
        nuitee_final_price=1200.0,
        nuitee_display_price=1000.0,
        booking_price=1350.0,
        fuzzy_score=95.0,
        match_status=MatchStatus.OK.value,
        tunnel_status=TunnelStatus.OK.value,
        currency="MAD",
    )
    defaults.update(overrides)
    return PriceRecord(**defaults).compute_derived()


class TestCleanDefinition:
    def test_fully_valid_record_is_clean(self, gate: QualityGate) -> None:
        record = gate.evaluate(make_record())
        assert record.is_clean is True
        assert record.quality_flags == ""

    @pytest.mark.parametrize(
        "overrides,expected_flag",
        [
            ({"match_status": MatchStatus.MATCH_FAIL.value}, QualityFlag.NO_MATCH),
            (
                {"tunnel_status": TunnelStatus.TUNNEL_BLOCKED.value},
                QualityFlag.TUNNEL_FAILED,
            ),
            ({"nuitee_final_price": None}, QualityFlag.MISSING_NUITEE_PRICE),
            ({"booking_price": None}, QualityFlag.MISSING_BOOKING_PRICE),
            ({"nuitee_final_price": 10.0}, QualityFlag.NUITEE_PRICE_OUT_OF_RANGE),
            ({"booking_price": 999_999.0}, QualityFlag.BOOKING_PRICE_OUT_OF_RANGE),
            ({"currency": "EUR"}, QualityFlag.CURRENCY_NOT_MAD),
            ({"fuzzy_score": 40.0}, QualityFlag.LOW_FUZZY_SCORE),
            ({"city": ""}, QualityFlag.MISSING_IDENTITY),
            ({"checkout": "2026-07-29"}, QualityFlag.INVALID_DATES),
        ],
    )
    def test_each_violation_is_flagged(
        self, gate: QualityGate, overrides: dict, expected_flag: str
    ) -> None:
        record = gate.evaluate(make_record(**overrides))
        assert record.is_clean is False
        assert expected_flag in record.quality_flags

    def test_implausible_ratio_rejected(self, gate: QualityGate) -> None:
        """A 20x gap is a mis-match, not a real price difference."""
        record = gate.evaluate(
            make_record(nuitee_final_price=200.0, booking_price=20_000.0)
        )
        assert QualityFlag.IMPLAUSIBLE_RATIO in record.quality_flags

    def test_partial_tunnel_still_counts_as_clean(self, gate: QualityGate) -> None:
        record = gate.evaluate(
            make_record(tunnel_status=TunnelStatus.PARTIAL.value)
        )
        assert record.is_clean is True

    def test_all_violations_are_reported_not_just_the_first(
        self, gate: QualityGate
    ) -> None:
        record = gate.evaluate(
            make_record(
                match_status=MatchStatus.ERROR.value,
                tunnel_status=TunnelStatus.ERROR.value,
                nuitee_final_price=None,
            )
        )
        flags = record.quality_flags.split(";")
        assert QualityFlag.NO_MATCH in flags
        assert QualityFlag.TUNNEL_FAILED in flags
        assert QualityFlag.MISSING_NUITEE_PRICE in flags


class TestWrongCountry:
    """C10: both sites agree on the hotel, but the search landed elsewhere."""

    @pytest.mark.parametrize(
        "city,url",
        [
            ("Rome", "https://www.booking.com/hotel/ro/margit-kulcsoshaz.html"),
            ("Faro", "https://www.booking.com/hotel/fo/hafnia.html"),
            ("Tangier", "https://www.booking.com/hotel/es/some-hotel.html"),
        ],
    )
    def test_booking_hotel_in_another_country_is_rejected(
        self, gate: QualityGate, city: str, url: str
    ) -> None:
        record = gate.evaluate(make_record(city=city, booking_url=url))
        assert QualityFlag.WRONG_COUNTRY in record.quality_flags

    @pytest.mark.parametrize(
        "city,url",
        [
            ("Rome", "https://www.booking.com/hotel/it/hotel-x.html"),
            ("London", "https://www.booking.com/hotel/gb/hotel-x.html"),
            ("Casablanca", "https://www.booking.com/hotel/ma/hotel-x.html"),
            ("Vienna", "https://www.booking.com/hotel/at/hotel-x.html"),  # unknown city
            ("Rome", None),  # no URL
        ],
    )
    def test_right_or_unknown_country_passes(
        self, gate: QualityGate, city: str, url: str
    ) -> None:
        record = gate.evaluate(make_record(city=city, booking_url=url))
        assert record.is_clean is True


class TestWholeStayPrice:
    """C11: a one-night price on a multi-night stay."""

    def _stay(self, **overrides) -> PriceRecord:
        values = dict(
            checkin="2026-11-11",
            checkout="2026-11-21",
            nights=10,
            nuitee_display_price=434.0,
            nuitee_final_price=4340.0,
            booking_price=4500.0,
        )
        values.update(overrides)
        return make_record(**values)

    def test_whole_stay_prices_pass(self, gate: QualityGate) -> None:
        assert gate.evaluate(self._stay()).is_clean is True

    def test_one_night_nuitee_price_is_rejected(self, gate: QualityGate) -> None:
        # Live: Merzouga 10n, Nuitee final 433.98 vs Booking 3000.
        record = gate.evaluate(self._stay(nuitee_final_price=433.98, booking_price=3000.0))
        assert QualityFlag.PRICE_NOT_WHOLE_STAY in record.quality_flags

    def test_one_night_booking_price_is_rejected(self, gate: QualityGate) -> None:
        # Live: Goreme 3n, display 870/night, Booking 767 for "3 nights".
        record = gate.evaluate(
            self._stay(
                checkout="2026-11-14", nights=3, nuitee_display_price=870.0,
                nuitee_final_price=2607.6, booking_price=767.0,
            )
        )
        assert QualityFlag.PRICE_NOT_WHOLE_STAY in record.quality_flags

    def test_single_night_stays_are_not_checked(self, gate: QualityGate) -> None:
        record = gate.evaluate(make_record(nuitee_final_price=300.0, booking_price=320.0))
        assert QualityFlag.PRICE_NOT_WHOLE_STAY not in record.quality_flags

    def test_check_can_be_disabled(self, settings: Settings) -> None:
        settings.quality.min_stay_price_fraction = 0.0
        record = QualityGate(settings).evaluate(self._stay(nuitee_final_price=433.98))
        assert QualityFlag.PRICE_NOT_WHOLE_STAY not in record.quality_flags


class TestDerivedFields:
    def test_booking_more_expensive(self) -> None:
        record = make_record(nuitee_final_price=1000.0, booking_price=1200.0)
        assert record.price_difference == 200.0
        assert record.price_difference_pct == 20.0
        assert record.cheaper_platform == "NUITEE"

    def test_nuitee_more_expensive(self) -> None:
        record = make_record(nuitee_final_price=1200.0, booking_price=1000.0)
        assert record.price_difference == -200.0
        assert record.cheaper_platform == "BOOKING"

    def test_tie(self) -> None:
        record = make_record(nuitee_final_price=1000.0, booking_price=1000.0)
        assert record.cheaper_platform == "TIE"

    def test_missing_price_leaves_derived_empty(self) -> None:
        record = make_record(booking_price=None)
        assert record.price_difference is None
        assert record.cheaper_platform is None

    def test_hidden_fees_from_tunnel(self) -> None:
        from src.models import TunnelResult

        result = TunnelResult(final_price=1200.0, display_price=1000.0)
        assert result.hidden_fees == 200.0


class TestDedupKey:
    def test_identical_inputs_collide(self) -> None:
        assert make_record().dedup_key == make_record().dedup_key

    @pytest.mark.parametrize(
        "overrides",
        [
            {"city": "Rabat"},
            {"checkin": "2026-08-01"},
            {"horizon_label": "J+7"},
            {"hotel_name_normalised": "other hotel"},
        ],
    )
    def test_distinct_inputs_differ(self, overrides: dict) -> None:
        assert make_record().dedup_key != make_record(**overrides).dedup_key

    def test_price_changes_do_not_change_the_key(self) -> None:
        """Re-scraping the same stay must UPSERT, not duplicate."""
        assert (
            make_record(booking_price=1.0).dedup_key
            == make_record(booking_price=9999.0).dedup_key
        )


class TestTextMatching:
    def test_accent_folding(self) -> None:
        assert strip_accents("Hôtel Médina") == "Hotel Medina"

    def test_noise_tokens_removed(self) -> None:
        assert normalise_key("Hôtel Barceló Anfa - Casablanca") == (
            "barcelo anfa casablanca"
        )

    def test_all_noise_name_falls_back_to_raw(self) -> None:
        assert normalise_key("The Hotel") != ""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Hotel Barcelo Anfa", "Barceló Anfa Hotel"),
            ("Sofitel Casablanca Tour Blanche", "Sofitel Tour Blanche Casablanca"),
            ("Riad Fes - Relais & Chateaux", "Riad Fes Relais et Chateaux"),
        ],
    )
    def test_equivalent_names_score_high(self, a: str, b: str) -> None:
        assert fuzzy_score(a, b) >= 85

    def test_unrelated_names_score_low(self) -> None:
        assert fuzzy_score("Hotel Barcelo Anfa", "Ibis Budget Marrakech") < 60

    def test_empty_inputs(self) -> None:
        assert fuzzy_score("", "Hotel") == 0.0
        assert fuzzy_score("Hotel", "") == 0.0
