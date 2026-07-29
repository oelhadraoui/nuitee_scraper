"""
src/verify_and_correct.py
=============================================================================
Verification and correction pass.

The scraping run is long and adversarial, so a fraction of rows always come
back incomplete. This pass re-attempts exactly those rows - and, per the new
requirements, it now handles BOTH failure surfaces:

    A. Failed / incomplete TUNNEL extractions
       (tunnel_status in {TUNNEL_BLOCKED, ERROR, PAYMENT_GUARD, NOT_ATTEMPTED},
        or nuitee_final_price missing)
       -> re-walk the Nuitee checkout tunnel from the stored detail URL.

    B. Failed Booking matches
       (match_status in {MATCH_FAIL, ERROR, NO_RESULTS}, or price missing)
       -> re-run the Booking lookup.

A row can need both, in which case both run and the record is rebuilt from
scratch before being re-gated.

It operates directly on the PriceStore (its in-memory query table is
bootstrapped from prices.csv - the actual source of truth on disk - at
start-up) and can also ingest a legacy CSV, so the CSVs produced by the
previous version of this project can be repaired and migrated in one
command.

Usage
-----
    python -m src.verify_and_correct --mode all --concurrency 4
    python -m src.verify_and_correct --mode tunnel --limit 500
    python -m src.verify_and_correct --import-csv output/legacy_prices.csv
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.config import Settings, get_settings
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
    parse_rooms_config,
)
from src.pipeline.quality import QualityGate
from src.pipeline.storage import PriceStore
from src.scrapers.booking import BookingScraper
from src.scrapers.nuitee import NuiteeScraper
from src.utils.browser import BrowserManager
from src.utils.logging_setup import get_logger, setup_logging
from src.utils.selectors import SelectorRegistry
from src.utils.text import normalise_key

log = get_logger(__name__)

__all__ = ["Verifier", "main"]


# ---------------------------------------------------------------------------
# Row selection predicates
# ---------------------------------------------------------------------------

_TUNNEL_FAILED_STATES = (
    TunnelStatus.TUNNEL_BLOCKED.value,
    TunnelStatus.ERROR.value,
    TunnelStatus.PAYMENT_GUARD.value,
    TunnelStatus.NOT_ATTEMPTED.value,
)

_BOOKING_FAILED_STATES = (
    MatchStatus.MATCH_FAIL.value,
    MatchStatus.ERROR.value,
    MatchStatus.NO_RESULTS.value,
    MatchStatus.SKIPPED.value,
)


def needs_tunnel_retry(row: dict[str, Any]) -> bool:
    """True when the Nuitee side of this row is incomplete."""
    if row.get("nuitee_final_price") in (None, "", 0):
        return True
    return str(row.get("tunnel_status") or "") in _TUNNEL_FAILED_STATES


def needs_booking_retry(row: dict[str, Any]) -> bool:
    """True when the Booking side of this row is incomplete."""
    if row.get("booking_price") in (None, "", 0):
        return True
    return str(row.get("match_status") or "") in _BOOKING_FAILED_STATES


def build_where_clause(mode: str) -> tuple[str, tuple]:
    """SQL predicate selecting the rows this mode should re-attempt."""
    tunnel_clause = (
        "(nuitee_final_price IS NULL OR nuitee_final_price <= 0 "
        "OR tunnel_status IN ({}))".format(
            ",".join("?" for _ in _TUNNEL_FAILED_STATES)
        )
    )
    booking_clause = (
        "(booking_price IS NULL OR booking_price <= 0 "
        "OR match_status IN ({}))".format(
            ",".join("?" for _ in _BOOKING_FAILED_STATES)
        )
    )

    if mode == "tunnel":
        return tunnel_clause, _TUNNEL_FAILED_STATES
    if mode == "booking":
        return booking_clause, _BOOKING_FAILED_STATES
    if mode == "unclean":
        return "is_clean = 0", ()
    # mode == "all"
    return (
        f"({tunnel_clause} OR {booking_clause})",
        _TUNNEL_FAILED_STATES + _BOOKING_FAILED_STATES,
    )


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

class Verifier:
    """Re-scrapes and repairs incomplete rows."""

    def __init__(self, settings: Settings, run_id: Optional[str] = None) -> None:
        self.settings = settings
        self.registry = SelectorRegistry.load(settings.selectors_path)
        self.store = PriceStore(settings, run_id=run_id or _verify_run_id())
        self.gate = QualityGate(settings)
        self.stats: dict[str, int] = {
            "examined": 0,
            "tunnel_retried": 0,
            "tunnel_fixed": 0,
            "booking_retried": 0,
            "booking_fixed": 0,
            "now_clean": 0,
            "still_dirty": 0,
        }

    # ======================================================================
    # Entry point
    # ======================================================================

    async def run(
        self,
        mode: str = "all",
        limit: Optional[int] = None,
        concurrency: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict[str, int]:
        self.store.init_schema()

        where, params = build_where_clause(mode)
        rows = await self.store.fetch(
            where=where, params=params, order_by="city, checkin", limit=limit
        )
        self.stats["examined"] = len(rows)

        if not rows:
            log.info("Nothing to verify in mode %r - the dataset is complete.", mode)
            return self.stats

        log.info(
            "Verification mode=%r: %d row(s) selected%s",
            mode, len(rows), " (DRY RUN)" if dry_run else "",
        )

        if dry_run:
            self._preview(rows)
            return self.stats

        workers = concurrency or self.settings.booking.concurrency
        async with BrowserManager(self.settings, concurrency=workers) as manager:
            nuitee = NuiteeScraper(manager, self.settings, self.registry)
            booking = BookingScraper(manager, self.settings, self.registry)

            semaphore = asyncio.Semaphore(workers)

            async def _one(row: dict[str, Any]) -> None:
                async with semaphore:
                    await self._repair_row(row, nuitee, booking)

            await asyncio.gather(*(_one(row) for row in rows), return_exceptions=True)

        await self.store.flush()
        self._log_summary()
        return self.stats

    # ======================================================================
    # Per-row repair
    # ======================================================================

    async def _repair_row(
        self, row: dict[str, Any], nuitee: NuiteeScraper, booking: BookingScraper
    ) -> None:
        window = self._window_from_row(row)
        hotel_name = str(row.get("hotel_name") or "").strip()
        if not hotel_name:
            self.stats["still_dirty"] += 1
            return

        tunnel = self._tunnel_from_row(row)
        booking_result = self._booking_from_row(row)

        # -- A. tunnel re-walk ------------------------------------------------
        if needs_tunnel_retry(row):
            self.stats["tunnel_retried"] += 1
            new_tunnel = await self._retry_tunnel(row, window, hotel_name, nuitee)
            if new_tunnel is not None and new_tunnel.final_price is not None:
                tunnel = new_tunnel
                self.stats["tunnel_fixed"] += 1
                log.info(
                    "[%s] tunnel repaired -> %.2f MAD",
                    hotel_name[:40], new_tunnel.final_price,
                )
            elif new_tunnel is not None:
                tunnel = new_tunnel

        # -- B. Booking re-lookup ---------------------------------------------
        # Only worth doing when a Nuitee price now exists; otherwise the row
        # cannot become clean regardless of the Booking outcome.
        if needs_booking_retry(row) and tunnel.final_price is not None:
            self.stats["booking_retried"] += 1
            new_booking = await booking.lookup(window, hotel_name)
            if new_booking.status is MatchStatus.OK and new_booking.price:
                booking_result = new_booking
                self.stats["booking_fixed"] += 1
                log.info(
                    "[%s] booking repaired -> %.2f MAD (score %.0f)",
                    hotel_name[:40], new_booking.price, new_booking.fuzzy_score,
                )
            elif new_booking.status is not MatchStatus.ERROR:
                booking_result = new_booking

        # -- rebuild, re-gate, persist ----------------------------------------
        record = self._rebuild_record(row, window, tunnel, booking_result)
        self.gate.evaluate(record)
        record.verified = True

        if record.is_clean:
            self.stats["now_clean"] += 1
        else:
            self.stats["still_dirty"] += 1

        await self.store.add(record)

    async def _retry_tunnel(
        self,
        row: dict[str, Any],
        window: SearchWindow,
        hotel_name: str,
        nuitee: NuiteeScraper,
    ) -> Optional[TunnelResult]:
        """
        Re-walk the tunnel.

        Prefers the stored detail URL. When the row has none (an old CSV, or a
        harvest that failed to capture the link) it re-harvests the city and
        looks the hotel up by normalised name - slower, but it recovers rows
        that would otherwise be permanently unfixable.
        """
        detail_url = str(row.get("nuitee_url") or "").strip()

        if detail_url:
            hotel = NuiteeHotel(
                name=hotel_name,
                display_price=_as_float(row.get("nuitee_display_price")),
                detail_url=detail_url,
            )
            results = await nuitee.price_hotels(window, [hotel])
            return results.get(normalise_key(hotel_name))

        log.debug("[%s] no stored detail URL - re-harvesting %s",
                  hotel_name[:40], window.city)
        harvested = await nuitee.harvest(window)
        target_key = normalise_key(hotel_name)
        match = next(
            (h for h in harvested if normalise_key(h.name) == target_key), None
        )
        if match is None:
            log.debug("[%s] not present in the fresh harvest", hotel_name[:40])
            return None

        results = await nuitee.price_hotels(window, [match])
        return results.get(target_key)

    # ======================================================================
    # Row <-> model conversion
    # ======================================================================

    @staticmethod
    def _window_from_row(row: dict[str, Any]) -> SearchWindow:
        rooms = parse_rooms_config(str(row.get("adults_per_room") or "2"))

        return SearchWindow.from_dict(
            {
                "city": row.get("city", ""),
                "checkin": row.get("checkin"),
                "checkout": row.get("checkout"),
                "horizon_label": row.get("horizon_label", "AD_HOC"),
                "horizon_offset_days": row.get("horizon_offset_days", 0) or 0,
                "execution_date": row.get("execution_date")
                or datetime.now().date().isoformat(),
                "rooms_config": list(rooms),
            }
        )

    @staticmethod
    def _tunnel_from_row(row: dict[str, Any]) -> TunnelResult:
        try:
            status = TunnelStatus(str(row.get("tunnel_status") or "NOT_ATTEMPTED"))
        except ValueError:
            status = TunnelStatus.NOT_ATTEMPTED
        return TunnelResult(
            status=status,
            final_price=_as_float(row.get("nuitee_final_price")),
            display_price=_as_float(row.get("nuitee_display_price")),
            local_fees=_as_float(row.get("nuitee_local_fees")),
            room_label=row.get("nuitee_room_label"),
            deepest_panel=str(row.get("tunnel_deepest_panel") or "unknown"),
            detail_url=row.get("nuitee_url"),
        )

    @staticmethod
    def _booking_from_row(row: dict[str, Any]) -> BookingResult:
        try:
            status = MatchStatus(str(row.get("match_status") or "SKIPPED"))
        except ValueError:
            status = MatchStatus.SKIPPED
        return BookingResult(
            status=status,
            price=_as_float(row.get("booking_price")),
            matched_name=row.get("booking_matched_name"),
            fuzzy_score=float(row.get("fuzzy_score") or 0.0),
            url=row.get("booking_url"),
        )

    def _rebuild_record(
        self,
        row: dict[str, Any],
        window: SearchWindow,
        tunnel: TunnelResult,
        booking: BookingResult,
    ) -> PriceRecord:
        hotel_name = str(row.get("hotel_name") or "")
        record = PriceRecord(
            hotel_name=hotel_name,
            hotel_name_normalised=normalise_key(hotel_name),
            city=window.city,
            checkin=window.checkin_str,
            checkout=window.checkout_str,
            horizon_label=window.horizon_label,
            horizon_offset_days=window.horizon_offset_days,
            execution_date=window.execution_date.isoformat(),
            nights=window.nights,
            rooms=window.rooms,
            adults_per_room=format_rooms_config(window.rooms_config),

            nuitee_display_price=tunnel.display_price,
            nuitee_final_price=tunnel.final_price,
            nuitee_hidden_fees=tunnel.local_fees
            if tunnel.local_fees is not None
            else compute_hidden_fees(
                tunnel.final_price, tunnel.display_price, window.nights
            ),
            nuitee_local_fees=tunnel.local_fees,
            nuitee_room_label=tunnel.room_label,
            tunnel_status=tunnel.status.value,
            tunnel_deepest_panel=tunnel.deepest_panel,
            tunnel_panels_visited=">".join(tunnel.panels_visited),
            nuitee_url=tunnel.detail_url or row.get("nuitee_url"),

            booking_price=booking.price,
            booking_matched_name=booking.matched_name,
            fuzzy_score=booking.fuzzy_score,
            match_status=booking.status.value,
            booking_url=booking.url or row.get("booking_url"),

            currency=self.settings.booking.currency,
            run_id=row.get("run_id") or self.store.run_id,
        )
        return record.compute_derived()

    # ======================================================================
    # Reporting
    # ======================================================================

    def _preview(self, rows: list[dict[str, Any]]) -> None:
        tunnel_n = sum(1 for r in rows if needs_tunnel_retry(r))
        booking_n = sum(1 for r in rows if needs_booking_retry(r))
        print(
            f"\nDRY RUN - {len(rows)} row(s) would be re-attempted:\n"
            f"  tunnel re-walks   : {tunnel_n}\n"
            f"  booking re-lookups: {booking_n}\n",
            flush=True,
        )
        for row in rows[:10]:
            print(
                f"  {str(row.get('hotel_name'))[:38]:<40} "
                f"{row.get('city'):<14} {row.get('checkin')} "
                f"tunnel={row.get('tunnel_status')} match={row.get('match_status')}"
            )
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")

    def _log_summary(self) -> None:
        s = self.stats
        print(
            "\n"
            + "=" * 64
            + "\n  VERIFICATION SUMMARY\n"
            + "=" * 64
            + f"\n  Rows examined       : {s['examined']:,}"
            + f"\n  Tunnel re-walks     : {s['tunnel_retried']:,} "
              f"(fixed {s['tunnel_fixed']:,})"
            + f"\n  Booking re-lookups  : {s['booking_retried']:,} "
              f"(fixed {s['booking_fixed']:,})"
            + f"\n  Now clean           : {s['now_clean']:,}"
            + f"\n  Still incomplete    : {s['still_dirty']:,}"
            + "\n"
            + "=" * 64
            + "\n",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Legacy CSV import
# ---------------------------------------------------------------------------

async def import_legacy_csv(path: Path, settings: Settings) -> int:
    """
    Ingest a CSV produced by the previous version of this project.

    The old schema had no tunnel columns, so every imported row is marked
    ``NOT_ATTEMPTED`` and will be picked up by a subsequent
    ``--mode tunnel`` pass. This is the migration path from the legacy
    dataset to the new final-payable-price dataset.
    """
    import csv as csv_module

    store = PriceStore(settings, run_id=f"import_{datetime.now():%Y%m%d_%H%M%S}")
    store.init_schema()
    gate = QualityGate(settings)

    imported = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv_module.DictReader(handle):
            row = {(k or "").strip(): v for k, v in raw.items()}
            hotel_name = (
                row.get("Hotel Name") or row.get("hotel_name") or ""
            ).strip()
            if not hotel_name:
                continue

            record = PriceRecord(
                hotel_name=hotel_name,
                hotel_name_normalised=normalise_key(hotel_name),
                city=(row.get("City") or row.get("city") or "").strip(),
                checkin=(row.get("Check-in") or row.get("checkin") or "").strip(),
                checkout=(row.get("Check-out") or row.get("checkout") or "").strip(),
                horizon_label=(row.get("Horizon") or "LEGACY").strip(),
                horizon_offset_days=0,
                execution_date=datetime.now().date().isoformat(),
                rooms=int(_as_float(row.get("Rooms")) or 1),
                adults_per_room=format_rooms_config(
                    parse_rooms_config(row.get("Adults per Room") or "2")
                ),

                nuitee_display_price=_as_float(row.get("Nuitee Price (MAD)")),
                nuitee_final_price=None,       # legacy rows never had one
                tunnel_status=TunnelStatus.NOT_ATTEMPTED.value,

                booking_price=_as_float(row.get("Booking Price (MAD)")),
                match_status=(row.get("match_status") or "SKIPPED").strip().upper(),
                booking_url=(row.get("Booking URL") or "").strip() or None,
                currency="MAD",
            )
            record.compute_derived()
            gate.evaluate(record)
            await store.add(record)
            imported += 1

    await store.flush()
    log.info(
        "Imported %d legacy row(s) from %s. Run "
        "`--mode tunnel` next to backfill final payable prices.",
        imported, path,
    )
    return imported


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_float(value: Any) -> Optional[float]:
    if value in (None, "", "N/A", "MATCH_FAIL"):
        return None
    try:
        return float(str(value).replace(",", "").replace("MAD", "").strip())
    except (TypeError, ValueError):
        return None


def _verify_run_id() -> str:
    return datetime.utcnow().strftime("verify_%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_and_correct",
        description="Re-scrape and repair incomplete tunnel / Booking extractions.",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "tunnel", "booking", "unclean"],
        default="all",
        help="Which failures to re-attempt (default: all)",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum rows to process")
    parser.add_argument("--concurrency", type=int, default=None,
                        help="Parallel workers (default: BOOKING_CONCURRENCY)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be re-attempted, then exit")
    parser.add_argument("--import-csv", type=Path, default=None,
                        help="Import a legacy CSV into the store, then exit")
    parser.add_argument("--export", action="store_true",
                        help="Re-export CSVs after verification")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging(
        level=args.log_level or settings.log_level,
        json_output=settings.log_json,
        log_file=settings.storage.output_dir / "verify_and_correct.log",
    )

    if args.import_csv is not None:
        if not args.import_csv.is_file():
            log.error("CSV not found: %s", args.import_csv)
            return 1
        await import_legacy_csv(args.import_csv, settings)
        return 0

    verifier = Verifier(settings)
    await verifier.run(
        mode=args.mode,
        limit=args.limit,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
    )

    if args.export and not args.dry_run:
        await verifier.store.export_csv(clean_only=False)
        await verifier.store.export_csv(clean_only=True)
        report = await verifier.gate.build_report(verifier.store)
        report.write(settings.storage.report_path)
        print(report.render(), flush=True)

    # A verification pass that repairs nothing is not a failure - there may
    # simply be nothing left to repair.
    return 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(asyncio.run(_async_main(args)))
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
