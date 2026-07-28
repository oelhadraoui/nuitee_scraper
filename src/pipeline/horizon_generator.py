"""
src/pipeline/horizon_generator.py
=============================================================================
Generation of the rolling multi-window analysis plan (Requirement C).

The concept
-----------
The client wants price competitiveness measured at two BOOKING LEAD TIMES,
not at two fixed dates:

    Horizon 1 (J+3) - a traveller booking three days before check-in
    Horizon 2 (J+7) - a traveller booking one week before check-in

A single pair of dates would only tell you about one week of the calendar. So
each horizon is swept across a rolling execution window: we simulate "the day
of scraping" advancing day by day across 2-3 months, and for each simulated
execution date D we derive:

    check-in  = D + offset          (offset = 3 or 7)
    check-out = check-in + nights

Concretely, with a 3-month window starting 2026-07-27:

    execution 2026-07-27, J+3 -> stay 2026-07-30 -> 2026-07-31
    execution 2026-07-28, J+3 -> stay 2026-07-31 -> 2026-08-01
    ...
    execution 2026-10-26, J+7 -> stay 2026-11-02 -> 2026-11-03

Every (execution date x horizon x city) triple becomes one SearchWindow.

Fallback
--------
Requirement C specifies a 2-3 month window with a fallback to 2 months when
inventory or availability is insufficient. ``plan_with_fallback`` implements
this as a measured decision, not a guess: it runs the 3-month plan, counts
clean rows, and if the yield per window falls below
``HORIZON_MIN_ROWS_PER_WINDOW`` it regenerates a denser 2-month plan (which
concentrates the same effort on nearer, better-stocked dates).
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Iterator, Optional, Sequence

from src.config import HorizonSettings
from src.models import SearchWindow
from src.utils.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "Horizon",
    "HorizonGenerator",
    "HorizonPlan",
    "add_months",
    "horizon_label",
]


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def add_months(start: date, months: int) -> date:
    """
    Add calendar months, clamping the day to the target month's length.

    ``add_months(date(2026, 1, 31), 1)`` -> ``2026-02-28``, not an error and
    not a March overflow.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return date(year, month, day)


def horizon_label(offset_days: int) -> str:
    """Canonical label for a lead time: 3 -> ``'J+3'``."""
    return f"J+{offset_days}"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Horizon:
    """One booking lead time."""

    offset_days: int
    nights: int = 1

    @property
    def label(self) -> str:
        return horizon_label(self.offset_days)

    def stay_for(self, execution_date: date) -> tuple[date, date]:
        """(check-in, check-out) for a given simulated scraping date."""
        checkin = execution_date + timedelta(days=self.offset_days)
        return checkin, checkin + timedelta(days=self.nights)


@dataclass(slots=True)
class HorizonPlan:
    """A fully-materialised execution plan."""

    windows: list[SearchWindow]
    months: int
    start_date: date
    end_date: date
    horizons: list[Horizon]
    cities: list[str]
    is_fallback: bool = False

    def __len__(self) -> int:
        return len(self.windows)

    def __iter__(self) -> Iterator[SearchWindow]:
        return iter(self.windows)

    def by_horizon(self, label: str) -> list[SearchWindow]:
        return [w for w in self.windows if w.horizon_label == label]

    def by_city(self, city: str) -> list[SearchWindow]:
        lowered = city.lower()
        return [w for w in self.windows if w.city.lower() == lowered]

    def summary(self) -> dict:
        return {
            "total_windows": len(self.windows),
            "months": self.months,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "horizons": [h.label for h in self.horizons],
            "cities": len(self.cities),
            "execution_dates": len({w.execution_date for w in self.windows}),
            "distinct_checkins": len({w.checkin for w in self.windows}),
            "is_fallback": self.is_fallback,
        }

    def describe(self) -> str:
        info = self.summary()
        return (
            f"{info['total_windows']} windows | "
            f"{info['cities']} cities x {len(info['horizons'])} horizons x "
            f"{info['execution_dates']} execution dates | "
            f"{info['start_date']} -> {info['end_date']}"
            f"{' (FALLBACK)' if self.is_fallback else ''}"
        )


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class HorizonGenerator:
    """Builds :class:`HorizonPlan` objects from configuration."""

    def __init__(self, settings: HorizonSettings) -> None:
        self.settings = settings

    # -- horizons -----------------------------------------------------------

    def horizons(self, only: Optional[Sequence[int]] = None) -> list[Horizon]:
        """Configured horizons, optionally filtered to specific offsets."""
        offsets = list(only) if only else list(self.settings.offsets_days)
        unknown = [o for o in offsets if o not in self.settings.offsets_days and only]
        if unknown:
            log.warning("horizon offsets %s are not in the configured set", unknown)
        return [Horizon(offset_days=o, nights=self.settings.nights) for o in offsets]

    # -- execution dates ----------------------------------------------------

    def execution_dates(
        self,
        start: Optional[date] = None,
        months: Optional[int] = None,
    ) -> list[date]:
        """
        The simulated scraping dates across the rolling window.

        Sampled every ``step_days``; the window end is always included so the
        far edge of the analysis period is never silently dropped.
        """
        start = start or date.today()
        months = months or self.settings.months
        end = add_months(start, months)
        step = max(1, self.settings.step_days)

        dates: list[date] = []
        cursor = start
        while cursor < end:
            dates.append(cursor)
            cursor += timedelta(days=step)

        last = end - timedelta(days=1)
        if dates and dates[-1] != last and last > dates[-1]:
            dates.append(last)

        return dates

    # -- plans --------------------------------------------------------------

    def build_plan(
        self,
        cities: Iterable[str],
        *,
        start: Optional[date] = None,
        months: Optional[int] = None,
        offsets: Optional[Sequence[int]] = None,
        rooms_config: Sequence[int] = (2,),
        is_fallback: bool = False,
    ) -> HorizonPlan:
        """
        Materialise the full cartesian plan:
        ``cities x horizons x execution dates``.

        Ordered execution-date-major so that a partial run still covers every
        city and both horizons for the dates it did reach - a run killed
        halfway is then still analysable, rather than being all-Paris.
        """
        city_list = [c.strip() for c in cities if c and c.strip()]
        if not city_list:
            raise ValueError("build_plan requires at least one city")

        start = start or date.today()
        months = months or self.settings.months
        horizon_list = self.horizons(offsets)
        exec_dates = self.execution_dates(start=start, months=months)
        rooms = tuple(int(r) for r in rooms_config)

        windows: list[SearchWindow] = []
        for execution_date in exec_dates:
            for horizon in horizon_list:
                checkin, checkout = horizon.stay_for(execution_date)
                for city in city_list:
                    windows.append(
                        SearchWindow(
                            city=city,
                            checkin=checkin,
                            checkout=checkout,
                            horizon_label=horizon.label,
                            horizon_offset_days=horizon.offset_days,
                            execution_date=execution_date,
                            rooms_config=rooms,
                        )
                    )

        plan = HorizonPlan(
            windows=windows,
            months=months,
            start_date=start,
            end_date=add_months(start, months),
            horizons=horizon_list,
            cities=city_list,
            is_fallback=is_fallback,
        )
        log.info("Horizon plan: %s", plan.describe())
        return plan

    def build_fallback_plan(
        self,
        cities: Iterable[str],
        *,
        start: Optional[date] = None,
        offsets: Optional[Sequence[int]] = None,
        rooms_config: Sequence[int] = (2,),
    ) -> HorizonPlan:
        """
        The denser 2-month plan used when the 3-month sweep under-delivers.

        Same total effort, concentrated on nearer dates where inventory is
        reliably present.
        """
        log.warning(
            "Falling back to a %d-month window (from %d)",
            self.settings.fallback_months, self.settings.months,
        )
        return self.build_plan(
            cities,
            start=start,
            months=self.settings.fallback_months,
            offsets=offsets,
            rooms_config=rooms_config,
            is_fallback=True,
        )

    # -- fallback decision --------------------------------------------------

    def should_fallback(self, clean_rows: int, windows_executed: int) -> bool:
        """
        Decide whether the wide window under-delivered.

        Compares realised clean rows per executed window against
        ``min_rows_per_window``. Requires a meaningful sample (>= 5 windows)
        so a run that failed for infrastructure reasons is not misdiagnosed as
        an inventory problem.
        """
        if windows_executed < 5:
            return False
        yield_per_window = clean_rows / windows_executed
        decision = yield_per_window < self.settings.min_rows_per_window
        log.info(
            "Fallback check: %.1f clean rows/window vs threshold %d -> %s",
            yield_per_window,
            self.settings.min_rows_per_window,
            "FALLBACK" if decision else "continue",
        )
        return decision


# ---------------------------------------------------------------------------
# Convenience: build a plan directly from an input.json city list
# ---------------------------------------------------------------------------

def plan_from_city_entries(
    entries: Sequence[dict],
    settings: HorizonSettings,
    *,
    start: Optional[date] = None,
    months: Optional[int] = None,
    offsets: Optional[Sequence[int]] = None,
) -> HorizonPlan:
    """
    Build a plan from ``input.json`` entries.

    Accepts both the legacy shape (``{"city", "checkin", "checkout"}``) and
    the new horizon-driven shape (``{"city", "rooms_config"}``). Legacy
    ``checkin``/``checkout`` values are ignored on purpose: dates are now
    derived from the horizon, which is the whole point of Requirement C.
    """
    generator = HorizonGenerator(settings)

    cities: list[str] = []
    rooms_config: tuple[int, ...] = (2,)
    seen: set[str] = set()

    for entry in entries:
        city = str(entry.get("city", "")).strip()
        if not city or city.lower() in seen:
            continue
        seen.add(city.lower())
        cities.append(city)

        if entry.get("rooms_config"):
            rooms_config = tuple(int(a) for a in entry["rooms_config"])
        elif "adults" in entry:
            adults = int(entry.get("adults", 2))
            n_rooms = int(entry.get("rooms", 1))
            rooms_config = tuple([adults] * n_rooms)

    return generator.build_plan(
        cities,
        start=start,
        months=months,
        offsets=offsets,
        rooms_config=rooms_config,
    )
