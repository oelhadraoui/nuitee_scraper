"""Tests for the today+offsets x durations analysis plan."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from src.config import HorizonSettings
from src.pipeline.horizon_generator import (
    HorizonGenerator,
    OffsetSpec,
    add_months,
    plan_from_city_entries,
)


@pytest.fixture
def settings() -> HorizonSettings:
    return HorizonSettings(
        short_offset_days=7,
        long_offset_months=3,
        long_offset_fallback_months=2,
        stay_durations_nights=[3, 10],
        min_rows_per_offset=10,
    )


@pytest.fixture
def generator(settings: HorizonSettings) -> HorizonGenerator:
    return HorizonGenerator(settings)


REF = date(2026, 7, 27)


class TestAddMonths:
    def test_simple(self) -> None:
        assert add_months(date(2026, 1, 15), 2) == date(2026, 3, 15)

    def test_clamps_to_short_month(self) -> None:
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_leap_year(self) -> None:
        assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)

    def test_crosses_year_boundary(self) -> None:
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


class TestOffsets:
    def test_both_offsets_by_default(self, generator: HorizonGenerator) -> None:
        specs = generator.offsets(REF)
        assert {s.kind for s in specs} == {"short", "long"}

    def test_short_offset_label_and_checkin(self, generator: HorizonGenerator) -> None:
        specs = generator.offsets(REF, only="short")
        assert len(specs) == 1
        spec = specs[0]
        assert spec.label == "1W"
        assert spec.checkin == date(2026, 8, 3)

    def test_long_offset_label_and_checkin(self, generator: HorizonGenerator) -> None:
        specs = generator.offsets(REF, only="long")
        assert len(specs) == 1
        spec = specs[0]
        assert spec.label == "3M"
        assert spec.checkin == add_months(REF, 3)

    def test_long_offset_fallback(self, generator: HorizonGenerator) -> None:
        specs = generator.offsets(REF, only="long", use_fallback_long_offset=True)
        spec = specs[0]
        assert spec.label == "2M"
        assert spec.checkin == add_months(REF, 2)

    def test_non_week_short_offset_label(self) -> None:
        gen = HorizonGenerator(HorizonSettings(short_offset_days=10))
        spec = gen.offsets(REF, only="short")[0]
        assert spec.label == "10D"

    def test_offset_spec_is_frozen(self) -> None:
        spec = OffsetSpec(label="1W", kind="short", checkin=REF)
        with pytest.raises(FrozenInstanceError):
            spec.label = "2W"  # type: ignore[misc]


class TestPlan:
    def test_cartesian_size(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Casablanca", "Marrakech"], reference_date=REF)
        # 2 offsets x 2 durations x 2 cities
        assert len(plan) == 2 * 2 * 2

    def test_both_offsets_present(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Rabat"], reference_date=REF)
        assert len(plan.by_horizon("1W")) == len(plan.by_horizon("3M")) > 0

    def test_checkout_after_checkin(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Agadir"], reference_date=REF)
        assert all(w.checkout > w.checkin for w in plan)

    def test_nights_match_duration(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Fes"], reference_date=REF)
        assert {w.nights for w in plan} == {3, 10}

    def test_only_short(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Tangier"], reference_date=REF, only="short")
        assert {w.horizon_label for w in plan} == {"1W"}
        assert len(plan) == 1 * 2 * 1  # 1 offset x 2 durations x 1 city

    def test_only_long(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Tangier"], reference_date=REF, only="long")
        assert {w.horizon_label for w in plan} == {"3M"}

    def test_custom_durations(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(
            ["Rabat"], reference_date=REF, durations=[1, 5, 14]
        )
        assert {w.nights for w in plan} == {1, 5, 14}
        assert plan.durations_nights == [1, 5, 14]

    def test_offset_major_ordering(self, generator: HorizonGenerator) -> None:
        """A truncated run must still cover every city for the offsets it reached."""
        plan = generator.build_plan(["A", "B"], reference_date=REF, durations=[3])
        first_two = plan.windows[:2]
        assert {w.city for w in first_two} == {"A", "B"}
        assert {w.horizon_label for w in first_two} == {"1W"}

    def test_empty_cities_rejected(self, generator: HorizonGenerator) -> None:
        with pytest.raises(ValueError):
            generator.build_plan([])

    def test_empty_durations_falls_back_to_settings(
        self, generator: HorizonGenerator
    ) -> None:
        """An empty/falsy durations override is treated like 'unspecified'."""
        plan = generator.build_plan(["Rabat"], reference_date=REF, durations=[])
        assert plan.durations_nights == list(generator.settings.stay_durations_nights)

    def test_rooms_config_propagates(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(
            ["Rabat"], reference_date=REF, rooms_config=(2, 3)
        )
        window = plan.windows[0]
        assert window.rooms == 2 and window.total_adults == 5

    def test_reference_date_defaults_to_today(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Rabat"])
        assert plan.reference_date == date.today()

    def test_fallback_flag_recorded(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(
            ["Rabat"], reference_date=REF, only="long", use_fallback_long_offset=True
        )
        assert plan.is_fallback is True
        assert plan.offset_labels == ["2M"]


class TestShouldFallbackLongOffset:
    def test_triggers_on_low_yield(self, generator: HorizonGenerator) -> None:
        assert generator.should_fallback_long_offset(
            long_offset_clean_rows=10, long_offset_windows=5
        ) is True

    def test_no_fallback_on_healthy_yield(self, generator: HorizonGenerator) -> None:
        assert generator.should_fallback_long_offset(
            long_offset_clean_rows=500, long_offset_windows=5
        ) is False

    def test_small_sample_never_triggers(self, generator: HorizonGenerator) -> None:
        """Infrastructure failure must not be misread as an inventory problem."""
        assert generator.should_fallback_long_offset(
            long_offset_clean_rows=0, long_offset_windows=2
        ) is False

    def test_fallback_months_clamped_not_rejected(self) -> None:
        """A narrower primary offset must clamp the fallback, not crash."""
        settings = HorizonSettings(
            long_offset_months=2, long_offset_fallback_months=3
        )
        assert settings.long_offset_fallback_months == 2
        assert HorizonSettings(long_offset_months=1).long_offset_fallback_months == 1


class TestPlanFromEntries:
    def test_legacy_format_dates_are_ignored(self, settings: HorizonSettings) -> None:
        entries = [
            {"city": "Paris", "checkin": "2026-08-01",
             "checkout": "2026-08-02", "adults": 2},
        ]
        plan = plan_from_city_entries(entries, settings, reference_date=REF)
        assert all(w.checkin != date(2026, 8, 1) or w.horizon_label for w in plan)
        assert {w.horizon_label for w in plan} == {"1W", "3M"}

    def test_deduplicates_cities(self, settings: HorizonSettings) -> None:
        entries = [{"city": "Rabat"}, {"city": "rabat"}, {"city": "Fes"}]
        plan = plan_from_city_entries(entries, settings, reference_date=REF)
        assert plan.cities == ["Rabat", "Fes"]

    def test_only_passed_through(self, settings: HorizonSettings) -> None:
        entries = [{"city": "Rabat"}]
        plan = plan_from_city_entries(
            entries, settings, reference_date=REF, only="short"
        )
        assert {w.horizon_label for w in plan} == {"1W"}
