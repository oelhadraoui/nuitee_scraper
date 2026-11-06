"""
Hard deadlines around whole units of work, and the per-scope store merge.

Live incident (2026-10): one Booking.com tab froze during "Orlando 1W". The
Playwright calls it was stuck in take no timeout, ``lookup_many``'s gather()
waited on it forever, and the run sat idle for five days. These tests pin
down that a unit of work that never returns now costs its deadline, not the
run - at every level (Booking attempt, tunnel walk, harvest, whole window,
context close).
"""

from __future__ import annotations

import asyncio
import csv
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import pytest

from src.config import Settings
from src.models import MatchStatus, NuiteeHotel, PriceRecord, TunnelStatus
from src.pipeline.horizon_generator import HorizonGenerator
from src.pipeline.runner import PipelineRunner
from src.pipeline.storage import PriceStore
from src.scrapers.booking import BookingScraper
from src.scrapers.nuitee import NuiteeScraper
from src.utils.browser import BrowserManager
from src.utils.selectors import SelectorRegistry
from src.utils.text import normalise_key


async def _forever(*_args, **_kwargs):
    await asyncio.Event().wait()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.storage.output_dir = tmp_path
    s.timeouts.booking_lookup_hard = 50
    s.timeouts.tunnel_hotel_hard = 50
    s.timeouts.harvest_hard = 50
    s.timeouts.window_hard = 200
    s.timeouts.context_close = 50
    return s


@pytest.fixture
def registry(settings: Settings) -> SelectorRegistry:
    return SelectorRegistry.load(settings.selectors_path)


@pytest.fixture
def window():
    plan = HorizonGenerator(Settings().horizon).build_plan(
        ["Orlando"], reference_date=date(2026, 10, 26), only="short", durations=[10]
    )
    return plan.windows[0]


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch) -> None:
    async def instant(*_a, **_k):
        return None

    monkeypatch.setattr("src.scrapers.booking.polite_delay", instant)


class TestBookingDeadline:
    async def test_frozen_lookup_returns_error_after_every_attempt(
        self, settings, registry, window, monkeypatch
    ) -> None:
        scraper = BookingScraper(manager=None, settings=settings, registry=registry)
        monkeypatch.setattr(scraper, "_lookup_once", _forever)

        result = await asyncio.wait_for(scraper.lookup(window, "Hyatt Regency"), 5)

        assert result.status is MatchStatus.ERROR
        assert "hard timeout" in (result.error or "")
        assert result.attempts == settings.booking.max_retries + 1

    async def test_one_frozen_hotel_does_not_block_the_batch(
        self, settings, registry, window, monkeypatch
    ) -> None:
        """The exact shape of the incident: N-1 lookups fine, one frozen."""
        from src.models import BookingResult

        scraper = BookingScraper(manager=None, settings=settings, registry=registry)

        async def lookup_once(_window, name, _identity=None):
            if name == "Frozen Hotel":
                await _forever()
            return BookingResult(status=MatchStatus.OK, price=1000.0)

        monkeypatch.setattr(scraper, "_lookup_once", lookup_once)
        names = ["A Hotel", "Frozen Hotel", "B Hotel"]

        results = await asyncio.wait_for(scraper.lookup_many(window, names), 5)

        assert results[normalise_key("A Hotel")].status is MatchStatus.OK
        assert results[normalise_key("B Hotel")].status is MatchStatus.OK
        assert results[normalise_key("Frozen Hotel")].status is MatchStatus.ERROR


class _FakeManager:
    @asynccontextmanager
    async def page(self, **_kwargs):
        yield object(), object()


class TestNuiteeDeadlines:
    async def test_frozen_tunnel_walk_is_abandoned(
        self, settings, registry, window, monkeypatch
    ) -> None:
        scraper = NuiteeScraper(_FakeManager(), settings, registry)
        monkeypatch.setattr("src.scrapers.nuitee.TunnelNavigator.run", _forever)
        hotel = NuiteeHotel("Frozen Hotel", display_price=900.0, detail_url="https://x/h")

        result = await asyncio.wait_for(scraper._price_one(window, hotel), 5)

        assert result.status is TunnelStatus.ERROR
        assert "hard timeout" in (result.error or "")

    async def test_frozen_harvest_yields_no_hotels(
        self, settings, registry, window, monkeypatch
    ) -> None:
        scraper = NuiteeScraper(_FakeManager(), settings, registry)
        monkeypatch.setattr(scraper, "_harvest", _forever)

        assert await asyncio.wait_for(scraper.harvest(window), 5) == []


class TestWindowDeadline:
    async def test_frozen_window_is_skipped_and_the_plan_continues(
        self, settings, monkeypatch
    ) -> None:
        plan = HorizonGenerator(Settings().horizon).build_plan(
            ["Orlando", "Toronto"], reference_date=date(2026, 10, 26),
            only="short", durations=[10],
        )
        runner = PipelineRunner(settings, run_id="watchdog")
        seen: list[str] = []

        async def execute_window(window, _nuitee, _booking):
            seen.append(window.city)
            if window.city == "Orlando":
                await _forever()

        monkeypatch.setattr(runner, "_execute_window", execute_window)
        await runner.store.start_run(plan.summary())

        await asyncio.wait_for(runner._execute_plan(plan, None, None), 5)

        assert seen == ["Orlando", "Toronto"]


class _HungContext:
    async def close(self):
        await _forever()


class TestContextClose:
    async def test_hung_close_is_bounded_and_schedules_a_recycle(self, settings) -> None:
        manager = BrowserManager(settings)

        await asyncio.wait_for(manager._close_context(_HungContext()), 5)

        assert manager._recycle_requested is True


def _record(name: str, city: str, clean: bool) -> PriceRecord:
    record = PriceRecord(
        hotel_name=name,
        hotel_name_normalised=normalise_key(name),
        city=city,
        checkin="2026-11-09",
        checkout="2026-11-12",
        horizon_label="1W",
        horizon_offset_days=7,
        execution_date="2026-11-02",
        nights=3,
        nuitee_final_price=1200.0,
        booking_price=1350.0 if clean else None,
        fuzzy_score=95.0,
        match_status=(MatchStatus.OK if clean else MatchStatus.MATCH_FAIL).value,
        tunnel_status=TunnelStatus.OK.value,
        currency="MAD",
    ).compute_derived()
    record.is_clean = clean
    return record


async def _store_in(base: Settings, folder: Path, records) -> PriceStore:
    s = base.model_copy(deep=True)
    s.storage.output_dir = folder
    store = PriceStore(s)
    await store.start_run({})
    for record in records:
        await store.add(record)
    await store.flush()
    return store


class TestMerge:
    async def test_scope_stores_merge_into_the_main_store(self, settings, tmp_path) -> None:
        await _store_in(settings, tmp_path / "national", [_record("Riad A", "Marrakech", True)])
        await _store_in(settings, tmp_path / "international", [
            _record("Hotel B", "Paris", True), _record("Hotel C", "Rome", False),
        ])
        main = await _store_in(settings, tmp_path, [_record("Old D", "Lisbon", True)])

        for scope in ("national", "international"):
            await main.import_csv(tmp_path / scope / "prices.csv")

        assert await main.count() == 4
        assert await main.clean_count() == 3
        with main.csv_path.open(encoding="utf-8-sig") as handle:
            cities = {row["City"] for row in csv.DictReader(handle)}
        assert cities == {"Marrakech", "Paris", "Rome", "Lisbon"}

    async def test_merge_never_downgrades_a_clean_row(self, settings, tmp_path) -> None:
        await _store_in(settings, tmp_path / "international", [_record("Hotel B", "Paris", False)])
        main = await _store_in(settings, tmp_path, [_record("Hotel B", "Paris", True)])

        await main.import_csv(tmp_path / "international" / "prices.csv")

        assert await main.clean_count() == 1
