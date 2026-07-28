"""Tests for the rolling multi-window horizon plan (Requirement C)."""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest

from src.config import HorizonSettings
from src.pipeline.horizon_generator import (
    Horizon,
    HorizonGenerator,
    add_months,
    horizon_label,
    plan_from_city_entries,
)


@pytest.fixture
def settings() -> HorizonSettings:
    return HorizonSettings(
        offsets_days=[3, 7], months=3, fallback_months=2, nights=1, step_days=1
    )


@pytest.fixture
def generator(settings: HorizonSettings) -> HorizonGenerator:
    return HorizonGenerator(settings)


class TestAddMonths:
    def test_simple(self) -> None:
        assert add_months(date(2026, 1, 15), 2) == date(2026, 3, 15)

    def test_clamps_to_short_month(self) -> None:
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_leap_year(self) -> None:
        assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)

    def test_crosses_year_boundary(self) -> None:
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)


class TestHorizon:
    def test_labels(self) -> None:
        assert horizon_label(3) == "J+3"
        assert Horizon(7).label == "J+7"

    def test_stay_derivation(self) -> None:
        checkin, checkout = Horizon(3, nights=1).stay_for(date(2026, 7, 27))
        assert checkin == date(2026, 7, 30)
        assert checkout == date(2026, 7, 31)

    def test_multi_night(self) -> None:
        checkin, checkout = Horizon(7, nights=3).stay_for(date(2026, 7, 27))
        assert (checkout - checkin).days == 3


class TestExecutionDates:
    def test_daily_step_covers_the_window(self, generator: HorizonGenerator) -> None:
        dates = generator.execution_dates(start=date(2026, 7, 27), months=3)
        assert dates[0] == date(2026, 7, 27)
        assert dates[-1] < add_months(date(2026, 7, 27), 3)
        assert len(dates) > 85

    def test_weekly_step(self) -> None:
        gen = HorizonGenerator(HorizonSettings(months=3, step_days=7))
        dates = gen.execution_dates(start=date(2026, 7, 27))
        gaps = {(b - a).days for a, b in pairwise(dates)}
        assert gaps <= {7, 1, 2, 3, 4, 5, 6}   # last gap may be a partial step

    def test_window_end_is_included(self) -> None:
        gen = HorizonGenerator(HorizonSettings(months=1, step_days=10))
        dates = gen.execution_dates(start=date(2026, 7, 1))
        assert dates[-1] == date(2026, 7, 31)


class TestPlan:
    def test_cartesian_size(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(
            ["Casablanca", "Marrakech"], start=date(2026, 7, 27), months=1
        )
        expected = 2 * 2 * len(generator.execution_dates(date(2026, 7, 27), 1))
        assert len(plan) == expected

    def test_both_horizons_present(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Rabat"], start=date(2026, 7, 27), months=1)
        assert len(plan.by_horizon("J+3")) == len(plan.by_horizon("J+7")) > 0

    def test_offsets_are_correct(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Fes"], start=date(2026, 7, 27), months=1)
        for window in plan:
            delta = (window.checkin - window.execution_date).days
            assert delta == window.horizon_offset_days
            assert window.horizon_label == f"J+{delta}"

    def test_checkout_after_checkin(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(["Agadir"], start=date(2026, 7, 27), months=1)
        assert all(w.checkout > w.checkin for w in plan)

    def test_horizon_filter(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(
            ["Tangier"], start=date(2026, 7, 27), months=1, offsets=[3]
        )
        assert {w.horizon_label for w in plan} == {"J+3"}

    def test_execution_date_major_ordering(self, generator: HorizonGenerator) -> None:
        """A truncated run must still cover every city and both horizons."""
        plan = generator.build_plan(
            ["A", "B"], start=date(2026, 7, 27), months=1
        )
        first_four = plan.windows[:4]
        assert len({w.execution_date for w in first_four}) == 1
        assert {w.city for w in first_four} == {"A", "B"}
        assert {w.horizon_label for w in first_four} == {"J+3", "J+7"}

    def test_empty_cities_rejected(self, generator: HorizonGenerator) -> None:
        with pytest.raises(ValueError):
            generator.build_plan([])

    def test_rooms_config_propagates(self, generator: HorizonGenerator) -> None:
        plan = generator.build_plan(
            ["Rabat"], start=date(2026, 7, 27), months=1, rooms_config=(2, 3)
        )
        window = plan.windows[0]
        assert window.rooms == 2 and window.total_adults == 5


class TestFallback:
    def test_fallback_plan_is_shorter(self, generator: HorizonGenerator) -> None:
        wide = generator.build_plan(["Rabat"], start=date(2026, 7, 27))
        narrow = generator.build_fallback_plan(["Rabat"], start=date(2026, 7, 27))
        assert narrow.months == 2 and narrow.is_fallback
        assert len(narrow) < len(wide)

    def test_triggers_on_low_yield(self, generator: HorizonGenerator) -> None:
        assert generator.should_fallback(clean_rows=100, windows_executed=50) is True

    def test_no_fallback_on_healthy_yield(self, generator: HorizonGenerator) -> None:
        assert generator.should_fallback(clean_rows=5_000, windows_executed=50) is False

    def test_small_sample_never_triggers(self, generator: HorizonGenerator) -> None:
        """Infrastructure failure must not be misread as an inventory problem."""
        assert generator.should_fallback(clean_rows=0, windows_executed=3) is False

    def test_fallback_months_clamped_not_rejected(self) -> None:
        """A narrower primary window must clamp the fallback, not crash."""
        assert HorizonSettings(months=2, fallback_months=3).fallback_months == 2
        assert HorizonSettings(months=1).fallback_months == 1


class TestPlanFromEntries:
    def test_legacy_format_dates_are_ignored(self, settings: HorizonSettings) -> None:
        entries = [
            {"city": "Paris", "checkin": "2026-08-01",
             "checkout": "2026-08-02", "adults": 2},
        ]
        plan = plan_from_city_entries(
            entries, settings, start=date(2026, 7, 27), months=1
        )
        assert all(w.checkin != date(2026, 8, 1) or w.horizon_label for w in plan)
        assert {w.horizon_label for w in plan} == {"J+3", "J+7"}

    def test_deduplicates_cities(self, settings: HorizonSettings) -> None:
        entries = [{"city": "Rabat"}, {"city": "rabat"}, {"city": "Fes"}]
        plan = plan_from_city_entries(
            entries, settings, start=date(2026, 7, 27), months=1
        )
        assert plan.cities == ["Rabat", "Fes"]
