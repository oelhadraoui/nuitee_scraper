"""
src/pipeline/horizon_generator.py
=============================================================================
Generation of the analysis plan: two offsets from today, each tested at two
stay durations.

The concept
-----------
Competitiveness is measured from a single reference date - "today", call it
T - at two different "how far ahead is this booking" offsets:

    Offset "short" - a traveller booking about a week out.
    Offset "long"  - a traveller booking a few months out (with its own
                     fallback to a nearer month if that offset's own results
                     turn out too thin - see ``should_fallback_long_offset``).

Both offsets are tested at BOTH of two stay lengths (a short 3-night stay and
a longer 10-night stay), so every city produces exactly
``2 offsets x 2 durations = 4`` search windows - not a sweep of many
simulated scraping dates. This keeps the plan's size proportional to the
number of cities and durations only, which is what actually makes a full run
finish in a practical amount of time.

    checkin(short) = T + HORIZON_SHORT_OFFSET_DAYS
    checkin(long)  = T + HORIZON_LONG_OFFSET_MONTHS months
                     (or HORIZON_LONG_OFFSET_FALLBACK_MONTHS, see below)
    checkout       = checkin + duration_nights

Fallback
--------
The long offset can turn out to have poor availability that far ahead. Rather
than guess, the pipeline runs the long offset for real, measures the clean
yield, and - only if that yield is too thin - re-runs just the long-offset
windows at the nearer fallback offset instead. The short offset's windows are
never affected by this decision.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Iterator, Optional, Sequence

from src.config import HorizonSettings
from src.models import SearchWindow
from src.utils.logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "HorizonGenerator",
    "HorizonPlan",
    "OffsetSpec",
    "add_months",
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


def _label_for_days(days: int) -> str:
    """``7`` -> ``"1W"``; anything not an exact number of weeks -> ``"{n}D"``."""
    if days > 0 and days % 7 == 0:
        return f"{days // 7}W"
    return f"{days}D"


def _label_for_months(months: int) -> str:
    return f"{months}M"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OffsetSpec:
    """One resolved 'how far ahead of today' offset."""

    label: str            # "1W", "3M", "2M", ...
    kind: str              # "short" | "long"
    checkin: date          # resolved against a specific reference date


@dataclass(slots=True)
class HorizonPlan:
    """A fully-materialised execution plan."""

    windows: list[SearchWindow]
    reference_date: date
    cities: list[str]
    offset_labels: list[str] = field(default_factory=list)
    durations_nights: list[int] = field(default_factory=list)
    is_fallback: bool = False
    #: The first ``priority_count`` entries of ``cities`` are the client's
    #: priority corridors; their windows all run before any other city's.
    priority_count: int = 0

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
            "reference_date": self.reference_date.isoformat(),
            "offsets": self.offset_labels,
            "stay_durations_nights": self.durations_nights,
            "cities": len(self.cities),
            "priority_cities": self.priority_count,
            "is_fallback": self.is_fallback,
        }

    def describe(self) -> str:
        info = self.summary()
        return (
            f"{info['total_windows']} windows | "
            f"{info['cities']} cities x {len(self.offset_labels)} offsets "
            f"({', '.join(self.offset_labels)}) x "
            f"{len(self.durations_nights)} durations "
            f"({', '.join(f'{n}n' for n in self.durations_nights)}) | "
            f"as of {info['reference_date']}"
            f"{' (FALLBACK)' if self.is_fallback else ''}"
        )


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class HorizonGenerator:
    """Builds :class:`HorizonPlan` objects from configuration."""

    def __init__(self, settings: HorizonSettings) -> None:
        self.settings = settings

    # -- offsets --------------------------------------------------------------

    def offsets(
        self,
        reference_date: date,
        *,
        use_fallback_long_offset: bool = False,
        only: Optional[str] = None,
    ) -> list[OffsetSpec]:
        """
        Resolve the configured offset(s) against ``reference_date``.

        ``only`` restricts to a single offset kind ("short" or "long") -
        used when re-running just the long offset at its fallback duration
        without re-running the short offset too.
        """
        specs: list[OffsetSpec] = []

        if only in (None, "short"):
            days = self.settings.short_offset_days
            specs.append(
                OffsetSpec(
                    label=_label_for_days(days),
                    kind="short",
                    checkin=reference_date + timedelta(days=days),
                )
            )

        if only in (None, "long"):
            months = (
                self.settings.long_offset_fallback_months
                if use_fallback_long_offset
                else self.settings.long_offset_months
            )
            specs.append(
                OffsetSpec(
                    label=_label_for_months(months),
                    kind="long",
                    checkin=add_months(reference_date, months),
                )
            )

        return specs

    # -- plans ------------------------------------------------------------------

    def build_plan(
        self,
        cities: Iterable[str],
        *,
        reference_date: Optional[date] = None,
        use_fallback_long_offset: bool = False,
        only: Optional[str] = None,
        durations: Optional[Sequence[int]] = None,
        rooms_config: Sequence[int] = (2,),
        priority_count: int = 0,
    ) -> HorizonPlan:
        """
        Materialise the full plan: ``cities x offsets x durations``.

        Ordered offset-major, then duration, then city, so a partial run
        still covers every city for the offset/duration pairs it reached.
        When ``priority_count`` is set, the first that many cities form a
        tier of their own that completes every offset x duration before the
        remaining cities start (each tier ordered as above).
        """
        city_list = [c.strip() for c in cities if c and c.strip()]
        if not city_list:
            raise ValueError("build_plan requires at least one city")

        reference_date = reference_date or date.today()
        duration_list = tuple(
            int(d) for d in (durations or self.settings.stay_durations_nights)
        )
        if not duration_list:
            raise ValueError("build_plan requires at least one stay duration")

        rooms = tuple(int(r) for r in rooms_config)
        offset_specs = self.offsets(
            reference_date,
            use_fallback_long_offset=use_fallback_long_offset,
            only=only,
        )

        priority_count = max(0, min(int(priority_count), len(city_list)))
        tiers = [city_list[:priority_count], city_list[priority_count:]]

        windows: list[SearchWindow] = []
        for tier in tiers:
            for offset in offset_specs:
                offset_days = (offset.checkin - reference_date).days
                for nights in duration_list:
                    checkout = offset.checkin + timedelta(days=nights)
                    for city in tier:
                        windows.append(
                            SearchWindow(
                                city=city,
                                checkin=offset.checkin,
                                checkout=checkout,
                                horizon_label=offset.label,
                                horizon_offset_days=offset_days,
                                execution_date=reference_date,
                                rooms_config=rooms,
                            )
                        )

        plan = HorizonPlan(
            windows=windows,
            reference_date=reference_date,
            cities=city_list,
            offset_labels=[o.label for o in offset_specs],
            durations_nights=list(duration_list),
            is_fallback=use_fallback_long_offset,
            priority_count=priority_count,
        )
        log.info("Analysis plan: %s", plan.describe())
        return plan

    # -- fallback decision --------------------------------------------------

    def should_fallback_long_offset(
        self, long_offset_clean_rows: int, long_offset_windows: int
    ) -> bool:
        """
        Decide whether the long offset under-delivered.

        Compares realised clean rows per long-offset window against
        ``min_rows_per_offset``. Requires a meaningful sample (>= 3 windows -
        i.e. at least a couple of city/duration combinations) so a run that
        failed for infrastructure reasons is not misdiagnosed as an inventory
        problem.
        """
        if long_offset_windows < 3:
            return False
        yield_per_window = long_offset_clean_rows / long_offset_windows
        decision = yield_per_window < self.settings.min_rows_per_offset
        log.info(
            "Long-offset fallback check: %.1f clean rows/window vs "
            "threshold %d -> %s",
            yield_per_window,
            self.settings.min_rows_per_offset,
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
    reference_date: Optional[date] = None,
    only: Optional[str] = None,
    durations: Optional[Sequence[int]] = None,
) -> HorizonPlan:
    """
    Build a plan from ``input.json`` entries.

    Accepts both the legacy shape (``{"city", "checkin", "checkout"}``) and
    the current shape (``{"city", "rooms_config"}``). Legacy
    ``checkin``/``checkout`` values are ignored on purpose: dates are always
    derived from today plus the configured offsets. Entries flagged
    ``"priority": true`` are moved to the front and run as their own tier.
    """
    generator = HorizonGenerator(settings)

    priority: list[str] = []
    others: list[str] = []
    rooms_config: tuple[int, ...] = (2,)
    seen: set[str] = set()

    for entry in entries:
        city = str(entry.get("city", "")).strip()
        if not city or city.lower() in seen:
            continue
        seen.add(city.lower())
        (priority if entry.get("priority") else others).append(city)

        if entry.get("rooms_config"):
            rooms_config = tuple(int(a) for a in entry["rooms_config"])
        elif "adults" in entry:
            adults = int(entry.get("adults", 2))
            n_rooms = int(entry.get("rooms", 1))
            rooms_config = tuple([adults] * n_rooms)

    return generator.build_plan(
        priority + others,
        reference_date=reference_date,
        only=only,
        durations=durations,
        rooms_config=rooms_config,
        priority_count=len(priority),
    )
