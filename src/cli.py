"""
src/cli.py
=============================================================================
Command-line interface.

Built on argparse rather than click so the package has one fewer runtime
dependency inside the Docker image; the subcommand structure is identical.

    python -m src.cli run      --only-offset all
    python -m src.cli plan     --dry-run
    python -m src.cli verify   --mode tunnel
    python -m src.cli report
    python -m src.cli export   --clean-only
    python -m src.cli doctor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence

from src.config import get_settings
from src.pipeline.horizon_generator import plan_from_city_entries
from src.pipeline.quality import QualityGate
from src.pipeline.runner import PipelineRunner
from src.pipeline.storage import PriceStore
from src.utils.logging_setup import get_logger, setup_logging
from src.utils.selectors import REQUIRED_SELECTOR_PATHS, SelectorRegistry

log = get_logger(__name__)

__all__ = ["build_parser", "main"]

DEFAULT_INPUT = Path("input.json")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nuitee-scraper",
        description=(
            "Nuitee vs Booking.com hotel price competitiveness pipeline "
            "with full checkout-tunnel price extraction."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m src.cli run --only-offset all\n"
            "  python -m src.cli run --only-offset long --limit-windows 20\n"
            "  python -m src.cli plan --dry-run\n"
            "  python -m src.cli verify --mode tunnel --limit 200\n"
            "  python -m src.cli report\n"
        ),
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override LOG_LEVEL",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- shared plan arguments ------------------------------------------------
    def add_plan_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--input", "-i", type=Path, default=DEFAULT_INPUT,
            help="Input JSON with the city list (default: input.json)",
        )
        sp.add_argument(
            "--only-offset", default="all", choices=["short", "long", "all"],
            help=(
                "Which offset(s) to run: 'short' (~1 week out), "
                "'long' (~3 months out), or 'all' (default)"
            ),
        )
        sp.add_argument(
            "--reference-date", type=_parse_date, default=None,
            help="The 'today' the offsets are measured from, YYYY-MM-DD "
                 "(default: actual today)",
        )
        sp.add_argument(
            "--durations", default=None,
            help="Comma-separated stay lengths in nights, overriding "
                 "HORIZON_STAY_DURATIONS_NIGHTS (e.g. '3,10')",
        )
        sp.add_argument(
            "--cities", default=None,
            help="Comma-separated city list, overriding --input",
        )
        sp.add_argument(
            "--limit-windows", type=int, default=None,
            help="Process at most N windows (useful for smoke tests)",
        )

    # -- run ------------------------------------------------------------------
    run_cmd = sub.add_parser("run", help="Execute the full scraping pipeline")
    add_plan_args(run_cmd)
    run_cmd.add_argument(
        "--output", "-o", type=Path, default=None,
        help="CSV output path (default: output/prices.csv)",
    )
    run_cmd.add_argument(
        "--target", type=int, default=None,
        help="Clean-hotel target, overriding QUALITY_TARGET_CLEAN_HOTELS",
    )
    run_cmd.add_argument(
        "--hotels-per-city", type=int, default=None,
        help="Hotels to price per city per date",
    )
    run_cmd.add_argument(
        "--no-tunnel", action="store_true",
        help="Skip the checkout tunnel and use display prices (diagnostics only)",
    )
    run_cmd.add_argument(
        "--no-resume", action="store_true",
        help="Re-scrape rows that are already stored clean",
    )
    run_cmd.add_argument(
        "--headed", action="store_true",
        help="Run Chromium with a visible window (local debugging)",
    )
    run_cmd.add_argument(
        "--run-id", default=None, help="Explicit run identifier",
    )
    run_cmd.add_argument(
        "--fail-under-target", action="store_true",
        help="Exit non-zero when the clean-hotel target is not reached",
    )

    # -- plan -----------------------------------------------------------------
    plan_cmd = sub.add_parser("plan", help="Show the horizon plan without scraping")
    add_plan_args(plan_cmd)
    plan_cmd.add_argument("--dry-run", action="store_true",
                          help="Print sample windows")
    plan_cmd.add_argument("--json", action="store_true",
                          help="Emit the plan as JSON")
    plan_cmd.add_argument("--save", type=Path, default=None,
                          help="Write the materialised plan to a JSON file")

    # -- verify ---------------------------------------------------------------
    verify_cmd = sub.add_parser("verify", help="Re-scrape incomplete rows")
    verify_cmd.add_argument(
        "--mode", choices=["all", "tunnel", "booking", "unclean"], default="all"
    )
    verify_cmd.add_argument("--limit", type=int, default=None)
    verify_cmd.add_argument("--concurrency", type=int, default=None)
    verify_cmd.add_argument("--dry-run", action="store_true")
    verify_cmd.add_argument("--export", action="store_true")

    # -- report ---------------------------------------------------------------
    report_cmd = sub.add_parser("report", help="Print the data quality report")
    report_cmd.add_argument("--json", action="store_true")
    report_cmd.add_argument("--save", type=Path, default=None)

    # -- export ---------------------------------------------------------------
    export_cmd = sub.add_parser("export", help="Export the store to CSV / Parquet")
    export_cmd.add_argument("--output", "-o", type=Path, default=None)
    export_cmd.add_argument("--clean-only", action="store_true")
    export_cmd.add_argument("--parquet", action="store_true")
    export_cmd.add_argument(
        "--raw-headers", action="store_true",
        help="Use machine column names instead of friendly headers",
    )

    # -- doctor ---------------------------------------------------------------
    sub.add_parser("doctor", help="Validate configuration, selectors and storage")

    return parser


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_only_offset(value: str) -> Optional[str]:
    """``'short'`` / ``'long'`` -> as given; ``'all'`` -> ``None`` (both)."""
    cleaned = (value or "all").strip().lower()
    return None if cleaned in ("all", "*", "") else cleaned


def _parse_durations(value: Optional[str]) -> Optional[list[int]]:
    """``'3,10'`` -> ``[3, 10]``; ``None``/empty -> ``None`` (use the default)."""
    if not value:
        return None
    return [int(part.strip()) for part in value.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Plan loading
# ---------------------------------------------------------------------------

def _load_cities(args: argparse.Namespace) -> list[dict]:
    if getattr(args, "cities", None):
        return [{"city": c.strip()} for c in args.cities.split(",") if c.strip()]

    path: Path = args.input
    if not path.is_file():
        raise SystemExit(
            f"Input file not found: {path}\n"
            f"Generate one with: python scripts/generate_input.py"
        )
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict):
        # New format: {"cities": [...], "rooms_config": [2]}
        entries = data.get("cities", [])
        rooms = data.get("rooms_config")
        normalised = []
        for entry in entries:
            item = {"city": entry} if isinstance(entry, str) else dict(entry)
            if rooms and "rooms_config" not in item:
                item["rooms_config"] = rooms
            normalised.append(item)
        return normalised

    return [{"city": e} if isinstance(e, str) else e for e in data]


def _build_plan(args: argparse.Namespace, settings):
    entries = _load_cities(args)

    plan = plan_from_city_entries(
        entries,
        settings.horizon,
        reference_date=getattr(args, "reference_date", None),
        only=_parse_only_offset(getattr(args, "only_offset", "all")),
        durations=_parse_durations(getattr(args, "durations", None)),
    )

    limit = getattr(args, "limit_windows", None)
    if limit and limit < len(plan.windows):
        plan.windows = plan.windows[:limit]
        log.info("Plan truncated to the first %d window(s)", limit)

    return plan


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()

    # CLI overrides beat the environment.
    if args.no_tunnel:
        settings.tunnel.enabled = False
    if args.no_resume:
        settings.storage.resume = False
    if args.headed:
        settings.browser.headless = False
    if args.target:
        settings.quality.target_clean_hotels = args.target
    if args.hotels_per_city:
        settings.nuitee.hotels_per_city = args.hotels_per_city
    if args.output:
        settings.storage.output_dir = args.output.parent
        settings.storage.csv_name = args.output.name

    plan = _build_plan(args, settings)
    if not plan.windows:
        log.error("The generated plan is empty - check --input and --only-offset")
        return 1

    runner = PipelineRunner(settings, run_id=args.run_id)
    outcome = await runner.run(plan)

    print(f"\nCSV        : {outcome.csv_path}")
    print(f"Clean CSV  : {outcome.clean_csv_path}")
    print(f"Report     : {settings.storage.report_path}")
    print(f"Database   : {settings.storage.database_path}\n")

    if args.fail_under_target and not outcome.target_met:
        log.error(
            "Clean-hotel target NOT met: %d / %d",
            outcome.report.distinct_clean_hotels,
            outcome.report.target_clean_hotels,
        )
        return 2
    if args.fail_under_target and not outcome.crossings_met:
        log.error(
            "Per-crossing minimum (%d clean hotels per offset x stay-duration) "
            "NOT met for every crossing - see 'by_crossing' in the quality report",
            outcome.report.min_clean_hotels_per_crossing,
        )
        return 2
    return 0


async def cmd_plan(args: argparse.Namespace) -> int:
    settings = get_settings()
    plan = _build_plan(args, settings)

    if args.json:
        print(json.dumps(plan.summary(), indent=2))
    else:
        print(f"\n{plan.describe()}\n")
        print(f"  Cities    : {', '.join(plan.cities[:12])}"
              f"{' ...' if len(plan.cities) > 12 else ''}")
        print(f"  Offsets   : {', '.join(plan.offset_labels)}")
        print(f"  Durations : {', '.join(f'{n}n' for n in plan.durations_nights)}")
        print(f"  Reference : {plan.reference_date}"
              f"{' (FALLBACK long offset)' if plan.is_fallback else ''}\n")

        if args.dry_run:
            print("  First 10 windows:")
            for window in plan.windows[:10]:
                print(
                    f"    exec {window.execution_date}  {window.horizon_label:<5} "
                    f"{window.checkin_str} -> {window.checkout_str}  {window.city}"
                )
            if len(plan.windows) > 10:
                print(f"    ... and {len(plan.windows) - 10} more\n")

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(
            json.dumps([w.to_dict() for w in plan.windows], indent=2),
            encoding="utf-8",
        )
        print(f"  Plan written to {args.save}\n")

    return 0


async def cmd_verify(args: argparse.Namespace) -> int:
    from src.verify_and_correct import Verifier

    settings = get_settings()
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
        print(report.render())

    return 0


async def cmd_report(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = PriceStore(settings)
    store.init_schema()

    report = await QualityGate(settings).build_report(store)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.render())

    report.write(args.save or settings.storage.report_path)
    # `report` is informational and always succeeds. Use
    # `run --fail-under-target` when you need a non-zero exit on a shortfall.
    return 0


async def cmd_export(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = PriceStore(settings)
    store.init_schema()

    path = await store.export_csv(
        path=args.output,
        clean_only=args.clean_only,
        friendly_headers=not args.raw_headers,
    )
    print(f"CSV exported to {path}")

    if args.parquet:
        parquet_path = await store.export_parquet()
        if parquet_path:
            print(f"Parquet exported to {parquet_path}")

    return 0


async def cmd_doctor(_: argparse.Namespace) -> int:
    """Pre-flight check: config, selectors, storage, optional dependencies."""
    problems: list[str] = []
    print("\nPre-flight check\n" + "-" * 60)

    # -- settings -----------------------------------------------------------
    try:
        settings = get_settings()
        print("  [ok]   settings loaded")
        print(f"         Nuitee URL      : {settings.nuitee.url}")
        print(f"         offsets         : "
              f"short={settings.horizon.short_offset_days}d, "
              f"long={settings.horizon.long_offset_months}mo "
              f"(fallback={settings.horizon.long_offset_fallback_months}mo)")
        print(f"         durations       : "
              f"{settings.horizon.stay_durations_nights} nights")
        print(f"         clean target    : {settings.quality.target_clean_hotels}")
        print(f"         output dir      : {settings.storage.output_dir}")
    except Exception as exc:
        print(f"  [FAIL] settings: {exc}")
        return 1

    # -- selectors ----------------------------------------------------------
    try:
        registry = SelectorRegistry.load(settings.selectors_path)
        missing = registry.validate(REQUIRED_SELECTOR_PATHS)
        if missing:
            problems.append(f"selectors missing: {', '.join(missing)}")
            print(f"  [FAIL] selectors: {len(missing)} required path(s) missing")
        else:
            print(f"  [ok]   selectors  : "
                  f"{len(REQUIRED_SELECTOR_PATHS)} required paths present")
    except Exception as exc:
        problems.append(f"selectors: {exc}")
        print(f"  [FAIL] selectors: {exc}")

    # -- storage ------------------------------------------------------------
    try:
        store = PriceStore(settings)
        store.init_schema()
        total = await store.count()
        clean = await store.clean_count()
        print(f"  [ok]   storage    : {total:,} rows ({clean:,} clean)")
    except Exception as exc:
        problems.append(f"storage: {exc}")
        print(f"  [FAIL] storage: {exc}")

    # -- optional dependencies ----------------------------------------------
    from src.utils.browser import HAS_STEALTH
    from src.utils.text import HAS_RAPIDFUZZ

    print(f"  [{'ok' if HAS_RAPIDFUZZ else 'WARN'}]   rapidfuzz  : "
          f"{'installed' if HAS_RAPIDFUZZ else 'MISSING - match quality will drop'}")
    print(f"  [{'ok' if HAS_STEALTH else 'WARN'}]   stealth    : "
          f"{'installed' if HAS_STEALTH else 'missing - higher block risk'}")

    try:
        from playwright.async_api import async_playwright  # noqa: F401
        print("  [ok]   playwright : importable")
    except ImportError as exc:
        problems.append(f"playwright: {exc}")
        print(f"  [FAIL] playwright: {exc}")

    print("-" * 60)
    if problems:
        print(f"  {len(problems)} problem(s) found.\n")
        return 1
    print("  All checks passed.\n")
    return 0


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_COMMANDS = {
    "run": cmd_run,
    "plan": cmd_plan,
    "verify": cmd_verify,
    "report": cmd_report,
    "export": cmd_export,
    "doctor": cmd_doctor,
}


async def _async_main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(
        level=args.log_level or settings.log_level,
        json_output=settings.log_json,
        log_file=settings.storage.output_dir / "scraper.log",
    )
    handler = _COMMANDS[args.command]
    return await handler(args)


def main(argv: Optional[Sequence[str]] = None) -> None:
    try:
        sys.exit(asyncio.run(_async_main(argv)))
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
