"""
src/pipeline/runner.py
=============================================================================
The orchestrator. Executes a HorizonPlan end to end.

Flow per SearchWindow
---------------------
    1. Nuitee harvest      - results page -> N hotels with display prices
    2. Nuitee tunnel walk   - each hotel -> TRUE FINAL PAYABLE PRICE
    3. Booking lookup       - fuzzy match + price for the same stay
    4. Assemble PriceRecord - derive difference / competitiveness
    5. Quality gate         - is_clean + quality_flags
    6. Persist              - buffered UPSERT into SQLite

Design decisions
----------------
* **Windows run sequentially, hotels run concurrently.** Both sites rate-limit
  per IP. Running two windows at once doubles the request rate without
  doubling throughput, because the bottleneck is the tunnel walk, which is
  already internally parallel.

* **Booking runs only for hotels the tunnel priced.** A Booking lookup for a
  hotel with no Nuitee final price can never produce a clean row, so spending
  a page load on it is pure waste.

* **Early exit on target.** Once the distinct-clean-hotel target is reached
  AND every (offset x stay-duration) crossing has independently reached
  ``QUALITY_MIN_CLEAN_HOTELS_PER_CROSSING``, the run stops. Hitting 1,000
  clean rows from a single crossing would satisfy the raw count but not the
  per-crossing statistical-weight requirement, so the global target alone is
  never sufficient to stop early.

* **Graceful shutdown.** SIGINT/SIGTERM set an event; the in-flight window
  finishes, buffers flush, exports run. You never lose six hours to a Ctrl-C.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime
from typing import Optional

from src.config import Settings
from src.models import (
    BookingResult,
    MatchStatus,
    NuiteeHotel,
    PriceRecord,
    SearchWindow,
    TunnelResult,
    TunnelStatus,
    compute_hidden_fees,
    format_rooms_config,
)
from src.pipeline.horizon_generator import HorizonGenerator, HorizonPlan
from src.pipeline.quality import QualityGate, QualityReport
from src.pipeline.storage import PriceStore
from src.scrapers.booking import BookingScraper
from src.scrapers.nuitee import NuiteeScraper
from src.utils.browser import BrowserManager
from src.utils.logging_setup import get_logger
from src.utils.selectors import (
    REQUIRED_SELECTOR_PATHS,
    SelectorRegistry,
)
from src.utils.text import normalise_key

log = get_logger(__name__)

__all__ = ["PipelineRunner", "RunOutcome"]


class RunOutcome:
    """Everything a caller needs after a run finishes."""

    def __init__(
        self,
        report: QualityReport,
        plan: HorizonPlan,
        windows_executed: int,
        csv_path: Optional[str] = None,
        clean_csv_path: Optional[str] = None,
        interrupted: bool = False,
    ) -> None:
        self.report = report
        self.plan = plan
        self.windows_executed = windows_executed
        self.csv_path = csv_path
        self.clean_csv_path = clean_csv_path
        self.interrupted = interrupted

    @property
    def target_met(self) -> bool:
        return self.report.target_met

    @property
    def crossings_met(self) -> bool:
        return self.report.crossings_met


class PipelineRunner:
    """Runs a horizon plan to completion (or to the clean-hotel target)."""

    def __init__(self, settings: Settings, run_id: Optional[str] = None) -> None:
        self.settings = settings
        self.registry = SelectorRegistry.load(settings.selectors_path)

        missing = self.registry.validate(REQUIRED_SELECTOR_PATHS)
        if missing:
            raise RuntimeError(
                "selectors.yaml is missing required paths: " + ", ".join(missing)
            )

        self.store = PriceStore(settings, run_id=run_id)
        self.gate = QualityGate(settings)
        self._shutdown = asyncio.Event()
        self._windows_executed = 0

    # ======================================================================
    # Entry point
    # ======================================================================

    async def run(self, plan: HorizonPlan) -> RunOutcome:
        """Execute ``plan``, with automatic long-offset fallback."""
        self._install_signal_handlers()
        await self.store.start_run(plan.summary())

        started = datetime.now()
        log.info("Starting run %s | %s", self.store.run_id, plan.describe())

        concurrency = max(
            self.settings.nuitee.tunnel_concurrency,
            self.settings.booking.concurrency,
        )

        async with BrowserManager(self.settings, concurrency=concurrency) as manager:
            nuitee = NuiteeScraper(manager, self.settings, self.registry)
            booking = BookingScraper(manager, self.settings, self.registry)

            await self._execute_plan(plan, nuitee, booking)

            # -- long-offset fallback decision -------------------------------
            # Only the long offset ("3M" by default) can trigger a fallback;
            # the short offset ("1W") is unaffected and is never re-run here.
            # ``build_plan`` always resolves offsets short-then-long, so the
            # second label (if present) is the long one.
            long_label = (
                plan.offset_labels[1] if len(plan.offset_labels) > 1 else None
            )

            if not self._shutdown.is_set() and not plan.is_fallback and long_label:
                generator = HorizonGenerator(self.settings.horizon)
                long_rows = await self.store.aggregate(
                    "SELECT COUNT(*) AS n, "
                    "SUM(CASE WHEN is_clean=1 THEN 1 ELSE 0 END) AS clean_n "
                    "FROM price_records WHERE horizon_label = ?",
                    (long_label,),
                )
                long_clean = int(long_rows[0]["clean_n"] or 0) if long_rows else 0
                long_windows = sum(
                    1 for w in plan.windows if w.horizon_label == long_label
                )
                needs_fallback = generator.should_fallback_long_offset(
                    long_clean, long_windows
                )
                if needs_fallback:
                    fallback_plan = generator.build_plan(
                        plan.cities,
                        reference_date=plan.reference_date,
                        use_fallback_long_offset=True,
                        only="long",
                        durations=plan.durations_nights,
                        rooms_config=(
                            plan.windows[0].rooms_config if plan.windows else (2,)
                        ),
                    )
                    log.warning(
                        "Long offset %r under-delivered (%d clean over %d "
                        "windows) - running the fallback offset %r instead",
                        long_label, long_clean, long_windows,
                        fallback_plan.offset_labels[0]
                        if fallback_plan.offset_labels else "?",
                    )
                    await self._execute_plan(fallback_plan, nuitee, booking)
                    plan = fallback_plan

        await self.store.finish_run()

        report = await self.gate.build_report(self.store)
        report.write(self.settings.storage.report_path)

        csv_path = await self.store.export_csv(clean_only=False)
        clean_csv_path = await self.store.export_csv(clean_only=True)

        elapsed = (datetime.now() - started).total_seconds()
        log.info(
            "Run %s finished in %.1f min (%d windows)",
            self.store.run_id, elapsed / 60, self._windows_executed,
        )
        print(report.render(), flush=True)

        self._log_selector_hits()

        return RunOutcome(
            report=report,
            plan=plan,
            windows_executed=self._windows_executed,
            csv_path=str(csv_path),
            clean_csv_path=str(clean_csv_path),
            interrupted=self._shutdown.is_set(),
        )

    # ======================================================================
    # Plan execution
    # ======================================================================

    async def _execute_plan(
        self, plan: HorizonPlan, nuitee: NuiteeScraper, booking: BookingScraper
    ) -> None:
        target = self.settings.quality.target_clean_hotels
        crossing_min = self.settings.quality.min_clean_hotels_per_crossing

        # Every (offset x stay-duration) pair this plan will touch, summed
        # across all cities. The global target alone is not the full
        # acceptance criterion - each of these crossings must independently
        # reach ``crossing_min`` too, so stopping early requires BOTH
        # conditions, not just the global one.
        expected_crossings = sorted(
            {(w.horizon_label, w.nights) for w in plan.windows}
        )

        for index, window in enumerate(plan.windows, start=1):
            if self._shutdown.is_set():
                log.warning("Shutdown requested - stopping after %d windows", index - 1)
                break

            clean_so_far = await self.store.clean_count()
            if clean_so_far >= target:
                shortfall = await self._crossing_shortfall(
                    expected_crossings, crossing_min
                )
                if not shortfall:
                    log.info(
                        "Target reached (%d/%d clean hotels) and every "
                        "offset x duration crossing has >= %d clean hotels - "
                        "stopping early",
                        clean_so_far, target, crossing_min,
                    )
                    break
                log.debug(
                    "Global target met (%d/%d) but %d crossing(s) still "
                    "below %d clean hotels (%s) - continuing",
                    clean_so_far, target, len(shortfall), crossing_min,
                    ", ".join(f"{label}/{nights}n" for label, nights in shortfall),
                )

            log.info(
                "[%d/%d] %s %s %s -> %s (clean so far: %d/%d)",
                index, len(plan.windows), window.city, window.horizon_label,
                window.checkin_str, window.checkout_str, clean_so_far, target,
            )

            try:
                await self._execute_window(window, nuitee, booking)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(
                    "[%s %s %s] window failed: %s",
                    window.city, window.horizon_label, window.checkin_str, exc,
                    exc_info=True,
                )

            self._windows_executed += 1
            await self.store.flush()

    async def _crossing_shortfall(
        self, expected_crossings: list[tuple[str, int]], crossing_min: int
    ) -> list[tuple[str, int]]:
        """
        (horizon_label, nights) pairs that have not yet reached
        ``crossing_min`` clean hotels, summed across every city.
        """
        rows = await self.store.aggregate(
            "SELECT horizon_label, nights, "
            "SUM(CASE WHEN is_clean=1 THEN 1 ELSE 0 END) AS clean_n "
            "FROM price_records GROUP BY horizon_label, nights"
        )
        counts = {
            (row["horizon_label"] or "", int(row["nights"] or 0)): int(
                row["clean_n"] or 0
            )
            for row in rows
        }
        return [
            crossing
            for crossing in expected_crossings
            if counts.get(crossing, 0) < crossing_min
        ]

    async def _execute_window(
        self, window: SearchWindow, nuitee: NuiteeScraper, booking: BookingScraper
    ) -> None:
        # -- 1. harvest -------------------------------------------------------
        hotels = await nuitee.harvest(window)
        if not hotels:
            log.warning(
                "[%s %s %s] no hotels harvested",
                window.city, window.horizon_label, window.checkin_str,
            )
            return

        # -- resume: drop work already completed cleanly ----------------------
        if self.settings.storage.resume:
            pending = [
                h for h in hotels
                if not self.store.already_done(
                    self._provisional_record(window, h).dedup_key
                )
            ]
            skipped = len(hotels) - len(pending)
            if skipped:
                log.info("[%s] resume: skipping %d completed hotels",
                         window.city, skipped)
            hotels = pending
            if not hotels:
                return

        # -- 2. tunnel --------------------------------------------------------
        tunnel_results = await nuitee.price_hotels(window, hotels)

        # -- 3. Booking, only for hotels with a usable Nuitee price -----------
        priceable = [
            h for h in hotels
            if (tunnel_results.get(normalise_key(h.name)) or TunnelResult()).final_price
            is not None
        ]
        log.debug(
            "[%s] %d/%d hotels have a final price - querying Booking",
            window.city, len(priceable), len(hotels),
        )

        booking_results = (
            await booking.lookup_many(window, [h.name for h in priceable])
            if priceable
            else {}
        )

        # -- 4/5/6. assemble, gate, persist -----------------------------------
        for hotel in hotels:
            key = normalise_key(hotel.name)
            record = self._build_record(
                window,
                hotel,
                tunnel_results.get(key) or TunnelResult(),
                booking_results.get(key) or BookingResult(status=MatchStatus.SKIPPED),
            )
            self.gate.evaluate(record)
            await self.store.add(record)

    # ======================================================================
    # Record assembly
    # ======================================================================

    def _provisional_record(
        self, window: SearchWindow, hotel: NuiteeHotel
    ) -> PriceRecord:
        """Minimal record used only to compute the resume dedup key."""
        return PriceRecord(
            hotel_name=hotel.name,
            hotel_name_normalised=normalise_key(hotel.name),
            city=window.city,
            checkin=window.checkin_str,
            checkout=window.checkout_str,
            horizon_label=window.horizon_label,
            horizon_offset_days=window.horizon_offset_days,
            execution_date=window.execution_date.isoformat(),
            rooms=window.rooms,
            adults_per_room=format_rooms_config(window.rooms_config),
        )

    def _build_record(
        self,
        window: SearchWindow,
        hotel: NuiteeHotel,
        tunnel: TunnelResult,
        booking: BookingResult,
    ) -> PriceRecord:
        record = PriceRecord(
            hotel_name=hotel.name,
            hotel_name_normalised=normalise_key(hotel.name),
            city=window.city,
            checkin=window.checkin_str,
            checkout=window.checkout_str,
            horizon_label=window.horizon_label,
            horizon_offset_days=window.horizon_offset_days,
            execution_date=window.execution_date.isoformat(),
            nights=window.nights,
            rooms=window.rooms,
            adults_per_room=format_rooms_config(window.rooms_config),

            nuitee_display_price=tunnel.display_price or hotel.display_price,
            nuitee_final_price=tunnel.final_price,
            # Prefer the fee actually scraped off the booking page; only
            # fall back to the nights-adjusted estimate when that line
            # couldn't be found for this hotel.
            nuitee_hidden_fees=tunnel.local_fees
            if tunnel.local_fees is not None
            else compute_hidden_fees(
                tunnel.final_price,
                tunnel.display_price or hotel.display_price,
                window.nights,
            ),
            nuitee_local_fees=tunnel.local_fees,
            nuitee_room_label=tunnel.room_label,
            tunnel_status=tunnel.status.value
            if isinstance(tunnel.status, TunnelStatus)
            else str(tunnel.status),
            tunnel_deepest_panel=tunnel.deepest_panel,
            tunnel_panels_visited=">".join(tunnel.panels_visited),
            nuitee_url=tunnel.detail_url or hotel.detail_url,

            booking_price=booking.price,
            booking_matched_name=booking.matched_name,
            fuzzy_score=booking.fuzzy_score,
            match_status=booking.status.value
            if isinstance(booking.status, MatchStatus)
            else str(booking.status),
            booking_url=booking.url,

            currency=self.settings.booking.currency,
        )
        return record.compute_derived()

    # ======================================================================
    # Lifecycle helpers
    # ======================================================================

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def _handle() -> None:
            if self._shutdown.is_set():
                log.warning("Second signal received - exiting immediately")
                raise SystemExit(130)
            log.warning(
                "Shutdown signal received - finishing the current window, "
                "then flushing and exporting. Send again to force-quit."
            )
            self._shutdown.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle)
            except (NotImplementedError, RuntimeError):
                # Windows / non-main thread - fall back to default handling.
                pass

    def _log_selector_hits(self) -> None:
        """
        Report which selector candidate actually fired for each cascade.

        This is the feedback loop for tuning ``selectors.yaml``: promote the
        winners, delete the candidates that never matched.
        """
        hits = self.registry.hit_report()
        if not hits:
            return
        log.debug("Selector hit report:")
        for path, candidates in sorted(hits.items()):
            winner = max(candidates.items(), key=lambda kv: kv[1])
            log.debug("  %-52s %s (%d hits)", path, winner[0][:48], winner[1])
