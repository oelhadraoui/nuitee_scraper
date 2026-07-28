"""
End-to-end orchestration test with the network mocked out.

Verifies the wiring that unit tests cannot: harvest -> tunnel -> Booking ->
record assembly -> quality gate -> persistence -> report -> CSV export, plus
the early-exit-on-target behaviour.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.config import Settings
from src.models import (
    BookingResult,
    MatchStatus,
    NuiteeHotel,
    TunnelResult,
    TunnelStatus,
)
from src.pipeline.horizon_generator import HorizonGenerator
from src.pipeline.quality import QualityGate
from src.pipeline.runner import PipelineRunner
from src.utils.text import normalise_key


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.storage.output_dir = tmp_path
    s.storage.batch_size = 5
    s.quality.target_clean_hotels = 3
    return s


class FakeNuitee:
    """Returns three hotels; the third always fails its tunnel walk."""

    def __init__(self) -> None:
        self.harvest_calls = 0
        self.tunnel_calls = 0

    async def harvest(self, window):
        self.harvest_calls += 1
        return [
            NuiteeHotel(
                f"Hotel {name}",
                display_price=900.0 + index * 100,
                detail_url=f"https://x/{name.lower()}",
            )
            for index, name in enumerate(("Alpha", "Beta", "Gamma"))
        ]

    async def price_hotels(self, window, hotels):
        self.tunnel_calls += 1
        results = {}
        for hotel in hotels:
            key = normalise_key(hotel.name)
            if "gamma" in key:
                results[key] = TunnelResult(
                    status=TunnelStatus.TUNNEL_BLOCKED,
                    display_price=hotel.display_price,
                    error="stalled on guest_details",
                )
            else:
                # display_price is a PER-NIGHT rate (matches the real site's
                # convention, live-verified 2026-07); final_price is the
                # stay total, so it scales with nights plus a fixed fee.
                results[key] = TunnelResult(
                    status=TunnelStatus.OK,
                    final_price=(hotel.display_price or 0) * window.nights + 250.0,
                    display_price=hotel.display_price,
                    room_label="Standard Double",
                    panels_visited=["room_select", "guest_details", "summary"],
                    deepest_panel="summary",
                    detail_url=hotel.detail_url,
                )
        return results


class FakeBooking:
    def __init__(self) -> None:
        self.queried: list[str] = []

    async def lookup_many(self, window, hotel_names):
        self.queried.extend(hotel_names)
        # Booking's card price is already a whole-stay total (live-verified
        # 2026-07), so it scales with nights just like Nuitee's final_price.
        return {
            normalise_key(name): BookingResult(
                status=MatchStatus.OK,
                price=1400.0 * window.nights,
                matched_name=name,
                fuzzy_score=97.0,
                url=f"https://booking.com/{normalise_key(name)}",
            )
            for name in hotel_names
        }


@pytest.fixture
def plan():
    """Two windows for one city: the short offset and the long offset,
    both at the same 3-night stay - i.e. two distinct crossings."""
    generator = HorizonGenerator(Settings().horizon)
    return generator.build_plan(
        ["Casablanca"], reference_date=date(2026, 7, 27), durations=[3]
    )


class TestOrchestration:
    async def test_full_window_pipeline(self, settings, plan) -> None:
        runner = PipelineRunner(settings, run_id="itest")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(plan.summary())
        await runner._execute_window(plan.windows[0], nuitee, booking)
        await runner.store.flush()

        rows = await runner.store.fetch()
        assert len(rows) == 3

        by_name = {r["hotel_name"]: r for r in rows}

        # Successful tunnel rows carry the FINAL payable price for the whole
        # stay (nights=3 for this window), not the per-night display price,
        # and record the nights-aware hidden-fee delta.
        alpha = by_name["Hotel Alpha"]
        assert alpha["nuitee_final_price"] == pytest.approx(900.0 * 3 + 250.0)
        assert alpha["nuitee_display_price"] == pytest.approx(900.0)
        assert alpha["nuitee_hidden_fees"] == pytest.approx(250.0)
        assert alpha["is_clean"] == 1
        assert alpha["tunnel_deepest_panel"] == "summary"
        assert alpha["tunnel_panels_visited"] == (
            "room_select>guest_details>summary"
        )

        # Comparison columns are derived correctly.
        assert alpha["booking_price"] == pytest.approx(1400.0 * 3)
        assert alpha["price_difference"] == pytest.approx(1400.0 * 3 - (900.0 * 3 + 250.0))
        assert alpha["cheaper_platform"] == "NUITEE"

        # The failed-tunnel hotel is retained, flagged, and NOT clean.
        gamma = by_name["Hotel Gamma"]
        assert gamma["is_clean"] == 0
        assert gamma["tunnel_status"] == TunnelStatus.TUNNEL_BLOCKED.value
        assert "TUNNEL_FAILED" in gamma["quality_flags"]

    async def test_booking_skipped_for_unpriced_hotels(
        self, settings, plan
    ) -> None:
        """Booking lookups must not be spent on hotels with no Nuitee price."""
        runner = PipelineRunner(settings, run_id="itest2")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(plan.summary())
        await runner._execute_window(plan.windows[0], nuitee, booking)

        assert "Hotel Gamma" not in booking.queried
        assert len(booking.queried) == 2

    async def test_resume_skips_completed_hotels(self, settings, plan) -> None:
        runner = PipelineRunner(settings, run_id="itest3")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(plan.summary())
        await runner._execute_window(plan.windows[0], nuitee, booking)
        await runner.store.flush()

        first_pass = len(booking.queried)
        await runner._execute_window(plan.windows[0], nuitee, booking)

        # The two clean hotels are skipped; only Gamma is re-attempted.
        assert len(booking.queried) == first_pass

    async def test_early_exit_once_target_is_met(self, settings) -> None:
        # Both windows share the SAME crossing (offset + stay-duration), only
        # the city differs - isolates the global-target behaviour from the
        # per-crossing check, which is covered separately below.
        generator = HorizonGenerator(Settings().horizon)
        single_crossing_plan = generator.build_plan(
            ["Casablanca", "Rabat"],
            reference_date=date(2026, 7, 27),
            only="short",
            durations=[3],
        )
        assert len(single_crossing_plan.windows) == 2
        assert {
            (w.horizon_label, w.nights) for w in single_crossing_plan.windows
        } == {("1W", 3)}

        settings.quality.target_clean_hotels = 2
        settings.quality.min_clean_hotels_per_crossing = 1
        runner = PipelineRunner(settings, run_id="itest4")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(single_crossing_plan.summary())
        await runner._execute_plan(single_crossing_plan, nuitee, booking)

        # Window 1 yields 2 clean hotels, hitting the global target, and its
        # crossing already clears the (trivial) per-crossing minimum, so no
        # later window ever runs.
        assert nuitee.harvest_calls == 1

    async def test_global_target_alone_does_not_trigger_early_exit(
        self, settings, plan
    ) -> None:
        """
        plan.windows[0] and [1] are the SAME city but DIFFERENT offsets
        (short then long) - two distinct crossings. Hitting the global target
        after window 1 must not skip window 2, because window 2's crossing
        (long offset x 3 nights) still has zero clean hotels against a real
        per-crossing minimum.
        """
        settings.quality.target_clean_hotels = 2
        settings.quality.min_clean_hotels_per_crossing = 200
        runner = PipelineRunner(settings, run_id="itest4b")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(plan.summary())
        await runner._execute_plan(plan, nuitee, booking)

        assert nuitee.harvest_calls == 2

    async def test_report_and_export(self, settings, plan) -> None:
        runner = PipelineRunner(settings, run_id="itest5")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(plan.summary())
        await runner._execute_window(plan.windows[0], nuitee, booking)
        await runner.store.flush()

        report = await QualityGate(settings).build_report(runner.store)
        assert report.total_rows == 3
        assert report.clean_rows == 2
        assert report.distinct_clean_hotels == 2
        assert report.by_flag["TUNNEL_FAILED"] == 1
        assert report.competitiveness["nuitee_cheaper"] == 2
        assert "DATA QUALITY REPORT" in report.render()

        csv_path = await runner.store.export_csv(clean_only=True)
        body = csv_path.read_text(encoding="utf-8-sig")
        assert "Hotel Alpha" in body and "Hotel Gamma" not in body
        assert body.splitlines()[0].split(",")[-1] == "Booking URL"

    async def test_by_crossing_report_tracks_each_offset_duration_pair(
        self, settings, plan
    ) -> None:
        """
        The second acceptance criterion (>= 200 clean hotels per offset x
        stay-duration crossing, summed across every city) needs the report to
        actually break the numbers out that way, not just globally.
        """
        runner = PipelineRunner(settings, run_id="itest7")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(plan.summary())
        for window in plan.windows:  # short-offset/3n and long-offset/3n
            await runner._execute_window(window, nuitee, booking)
        await runner.store.flush()

        report = await QualityGate(settings).build_report(runner.store)

        assert set(report.by_crossing) == {"1W|3n", "3M|3n"}
        for stats in report.by_crossing.values():
            assert stats["clean"] == 2  # Alpha + Beta clean per window

        # Default per-crossing minimum (200) is nowhere near met by 2 rows.
        assert report.crossings_met is False

        report.min_clean_hotels_per_crossing = 2
        assert report.crossings_met is True

    async def test_horizon_is_recorded_on_every_row(self, settings, plan) -> None:
        runner = PipelineRunner(settings, run_id="itest6")
        nuitee, booking = FakeNuitee(), FakeBooking()

        await runner.store.start_run(plan.summary())
        for window in plan.windows:
            await runner._execute_window(window, nuitee, booking)
        await runner.store.flush()

        rows = await runner.store.fetch()
        assert {r["horizon_label"] for r in rows} == {"1W", "3M"}
        for row in rows:
            offset = (
                date.fromisoformat(row["checkin"])
                - date.fromisoformat(row["execution_date"])
            ).days
            assert offset == row["horizon_offset_days"]
