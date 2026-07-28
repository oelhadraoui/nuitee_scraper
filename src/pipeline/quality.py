"""
src/pipeline/quality.py
=============================================================================
The data-quality gate and reporting layer (Requirement B).

"At least 1,000 clean hotels" is only a meaningful acceptance criterion if
"clean" is defined precisely and applied mechanically. This module is that
definition, in code.

A record is CLEAN when every one of these holds:

    C1  match_status == OK                (the hotel was actually matched)
    C2  tunnel_status in {OK, PARTIAL}    (a real payable price was reached)
    C3  nuitee_final_price is present and within [min_price, max_price]
    C4  booking_price     is present and within [min_price, max_price]
    C5  currency == MAD                   (normalisation succeeded)
    C6  fuzzy_score >= the configured threshold
    C7  price ratio between the two sites <= max_price_ratio
    C8  all identity fields present (hotel, city, checkin, checkout, horizon)
    C9  checkout > checkin                (internally consistent stay)

Anything failing one or more checks is retained with ``is_clean = 0`` and a
``quality_flags`` string listing exactly which checks failed. Nothing is
silently dropped: the rejects are the diagnostic trail that tells you whether
your shortfall is a matching problem, a tunnel problem, or an inventory
problem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.config import Settings
from src.models import MatchStatus, PriceRecord, TunnelStatus
from src.utils.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["QualityFlag", "QualityGate", "QualityReport"]


class QualityFlag:
    """Canonical rejection reasons. Stable strings - they end up in the CSV."""

    NO_MATCH = "NO_MATCH"
    TUNNEL_FAILED = "TUNNEL_FAILED"
    MISSING_NUITEE_PRICE = "MISSING_NUITEE_PRICE"
    MISSING_BOOKING_PRICE = "MISSING_BOOKING_PRICE"
    NUITEE_PRICE_OUT_OF_RANGE = "NUITEE_PRICE_OUT_OF_RANGE"
    BOOKING_PRICE_OUT_OF_RANGE = "BOOKING_PRICE_OUT_OF_RANGE"
    CURRENCY_NOT_MAD = "CURRENCY_NOT_MAD"
    LOW_FUZZY_SCORE = "LOW_FUZZY_SCORE"
    IMPLAUSIBLE_RATIO = "IMPLAUSIBLE_RATIO"
    MISSING_IDENTITY = "MISSING_IDENTITY"
    INVALID_DATES = "INVALID_DATES"


@dataclass(slots=True)
class QualityReport:
    """Aggregated quality metrics for a run."""

    generated_at: str = field(
        default_factory=lambda: datetime.utcnow().isoformat()
    )
    run_id: str = ""
    target_clean_hotels: int = 1_000
    min_clean_hotels_per_crossing: int = 200

    total_rows: int = 0
    clean_rows: int = 0
    distinct_clean_hotels: int = 0

    by_flag: dict[str, int] = field(default_factory=dict)
    by_match_status: dict[str, int] = field(default_factory=dict)
    by_tunnel_status: dict[str, int] = field(default_factory=dict)
    by_horizon: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_city: dict[str, dict[str, Any]] = field(default_factory=dict)

    #: The second acceptance criterion: clean-hotel count for every
    #: (offset x stay-duration) crossing, summed across every city, keyed
    #: ``"Offset|Nnights"`` (e.g. ``"3M|10n"``).
    by_crossing: dict[str, dict[str, Any]] = field(default_factory=dict)

    price_stats: dict[str, Any] = field(default_factory=dict)
    competitiveness: dict[str, Any] = field(default_factory=dict)

    @property
    def clean_rate(self) -> float:
        return (self.clean_rows / self.total_rows * 100.0) if self.total_rows else 0.0

    @property
    def target_met(self) -> bool:
        return self.distinct_clean_hotels >= self.target_clean_hotels

    @property
    def crossings_met(self) -> bool:
        """True only if EVERY observed crossing clears the per-crossing minimum."""
        if not self.by_crossing:
            return False
        return all(
            stats.get("clean", 0) >= self.min_clean_hotels_per_crossing
            for stats in self.by_crossing.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "target": {
                "target_clean_hotels": self.target_clean_hotels,
                "distinct_clean_hotels": self.distinct_clean_hotels,
                "met": self.target_met,
                "shortfall": max(
                    0, self.target_clean_hotels - self.distinct_clean_hotels
                ),
            },
            "crossings": {
                "min_clean_hotels_per_crossing": self.min_clean_hotels_per_crossing,
                "met": self.crossings_met,
                "by_crossing": self.by_crossing,
            },
            "volume": {
                "total_rows": self.total_rows,
                "clean_rows": self.clean_rows,
                "clean_rate_pct": round(self.clean_rate, 2),
            },
            "rejections_by_flag": self.by_flag,
            "match_status_breakdown": self.by_match_status,
            "tunnel_status_breakdown": self.by_tunnel_status,
            "by_horizon": self.by_horizon,
            "by_city": self.by_city,
            "price_stats": self.price_stats,
            "competitiveness": self.competitiveness,
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("Quality report written to %s", path)
        return path

    def render(self) -> str:
        """Human-readable summary for the console / CI log."""
        lines = [
            "",
            "=" * 72,
            "  DATA QUALITY REPORT",
            "=" * 72,
            f"  Run                     : {self.run_id}",
            f"  Total rows              : {self.total_rows:,}",
            f"  Clean rows              : {self.clean_rows:,} "
            f"({self.clean_rate:.1f}%)",
            f"  Distinct clean hotels   : {self.distinct_clean_hotels:,}",
            f"  Target                  : {self.target_clean_hotels:,}",
            f"  Status                  : "
            f"{'MET' if self.target_met else 'NOT MET'}",
        ]

        if not self.target_met:
            shortfall = self.target_clean_hotels - self.distinct_clean_hotels
            lines.append(f"  Shortfall               : {shortfall:,} hotels")

        lines.append(
            f"  Per-crossing minimum    : {self.min_clean_hotels_per_crossing:,} "
            f"clean hotels ({'MET' if self.crossings_met else 'NOT MET'} for all crossings)"
        )

        if self.by_crossing:
            lines += ["", "  By crossing (offset x stay-duration):"]
            for label, stats in sorted(self.by_crossing.items()):
                clean = stats.get("clean", 0)
                ok = clean >= self.min_clean_hotels_per_crossing
                lines.append(
                    f"    {label:<28} clean={clean:>6,} / "
                    f"{self.min_clean_hotels_per_crossing:<5,} "
                    f"{'OK' if ok else 'SHORT'}"
                )

        if self.by_flag:
            lines += ["", "  Top rejection reasons:"]
            for flag, count in sorted(
                self.by_flag.items(), key=lambda kv: kv[1], reverse=True
            )[:8]:
                share = count / self.total_rows * 100 if self.total_rows else 0
                lines.append(f"    {flag:<28} {count:>7,}  ({share:5.1f}%)")

        if self.by_horizon:
            lines += ["", "  By horizon:"]
            for label, stats in sorted(self.by_horizon.items()):
                lines.append(
                    f"    {label:<8} rows={stats.get('rows', 0):>7,}  "
                    f"clean={stats.get('clean', 0):>7,}  "
                    f"hotels={stats.get('distinct_hotels', 0):>6,}"
                )

        if self.competitiveness:
            comp = self.competitiveness
            lines += [
                "",
                "  Price competitiveness (clean rows only):",
                f"    Nuitee cheaper          : {comp.get('nuitee_cheaper', 0):,} "
                f"({comp.get('nuitee_cheaper_pct', 0)}%)",
                f"    Booking cheaper         : {comp.get('booking_cheaper', 0):,} "
                f"({comp.get('booking_cheaper_pct', 0)}%)",
                f"    Tie                     : {comp.get('tie', 0):,}",
                f"    Mean difference (MAD)   : {comp.get('mean_difference', 0)}",
                f"    Median difference (MAD) : {comp.get('median_difference', 0)}",
            ]

        lines += ["=" * 72, ""]
        return "\n".join(lines)


class QualityGate:
    """Applies the clean-row definition to individual records."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._q = settings.quality
        self._min = settings.quality.min_price_mad
        self._max = settings.quality.max_price_mad
        self._fuzzy_min = settings.booking.fuzzy_threshold

    # ======================================================================
    # Per-record evaluation
    # ======================================================================

    def evaluate(self, record: PriceRecord) -> PriceRecord:
        """
        Set ``is_clean`` and ``quality_flags`` on ``record`` (in place).

        Runs every check rather than short-circuiting: knowing that a row
        failed on BOTH the tunnel and the match is diagnostically much more
        useful than knowing it failed on the first check.
        """
        flags: list[str] = []

        # -- C8: identity -------------------------------------------------
        if not all(
            [
                record.hotel_name.strip(),
                record.city.strip(),
                record.checkin,
                record.checkout,
                record.horizon_label,
            ]
        ):
            flags.append(QualityFlag.MISSING_IDENTITY)

        # -- C9: date consistency ------------------------------------------
        if not _dates_valid(record.checkin, record.checkout):
            flags.append(QualityFlag.INVALID_DATES)

        # -- C1: Booking match ---------------------------------------------
        if record.match_status != MatchStatus.OK.value:
            flags.append(QualityFlag.NO_MATCH)

        # -- C2: tunnel ------------------------------------------------------
        if record.tunnel_status not in (
            TunnelStatus.OK.value,
            TunnelStatus.PARTIAL.value,
        ):
            flags.append(QualityFlag.TUNNEL_FAILED)

        # -- C3 / C4: prices present and in range ---------------------------
        if record.nuitee_final_price is None:
            flags.append(QualityFlag.MISSING_NUITEE_PRICE)
        elif not self._min <= record.nuitee_final_price <= self._max:
            flags.append(QualityFlag.NUITEE_PRICE_OUT_OF_RANGE)

        if record.booking_price is None:
            flags.append(QualityFlag.MISSING_BOOKING_PRICE)
        elif not self._min <= record.booking_price <= self._max:
            flags.append(QualityFlag.BOOKING_PRICE_OUT_OF_RANGE)

        # -- C5: currency ----------------------------------------------------
        if (record.currency or "").upper() != "MAD":
            flags.append(QualityFlag.CURRENCY_NOT_MAD)

        # -- C6: match confidence -------------------------------------------
        if (
            record.match_status == MatchStatus.OK.value
            and record.fuzzy_score < self._fuzzy_min
        ):
            flags.append(QualityFlag.LOW_FUZZY_SCORE)

        # -- C7: plausibility -------------------------------------------------
        if record.nuitee_final_price and record.booking_price:
            high = max(record.nuitee_final_price, record.booking_price)
            low = min(record.nuitee_final_price, record.booking_price)
            if low > 0 and (high / low) > self._q.max_price_ratio:
                flags.append(QualityFlag.IMPLAUSIBLE_RATIO)

        record.quality_flags = ";".join(flags)
        record.is_clean = not flags
        return record

    def evaluate_all(self, records: list[PriceRecord]) -> list[PriceRecord]:
        return [self.evaluate(record) for record in records]

    # ======================================================================
    # Reporting
    # ======================================================================

    async def build_report(self, store, run_scoped: bool = False) -> QualityReport:
        """Compute the full report with SQL aggregates (never loads all rows)."""
        await store.flush()

        report = QualityReport(
            run_id=store.run_id,
            target_clean_hotels=self._q.target_clean_hotels,
            min_clean_hotels_per_crossing=self._q.min_clean_hotels_per_crossing,
        )
        scope = "WHERE run_id = ?" if run_scoped else ""
        params: tuple = (store.run_id,) if run_scoped else ()

        # -- volume ---------------------------------------------------------
        totals = await store.aggregate(
            f"SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN is_clean=1 THEN 1 ELSE 0 END) AS clean "
            f"FROM price_records {scope}",
            params,
        )
        if totals:
            report.total_rows = int(totals[0]["total"] or 0)
            report.clean_rows = int(totals[0]["clean"] or 0)

        # -- THE headline metric: distinct clean hotels ---------------------
        distinct = await store.aggregate(
            f"SELECT COUNT(DISTINCT hotel_name_normalised || '|' || city) AS n "
            f"FROM price_records "
            f"WHERE is_clean = 1 {'AND run_id = ?' if run_scoped else ''}",
            params,
        )
        if distinct:
            report.distinct_clean_hotels = int(distinct[0]["n"] or 0)

        # -- rejection reasons ----------------------------------------------
        flag_rows = await store.aggregate(
            f"SELECT quality_flags FROM price_records "
            f"WHERE is_clean = 0 {'AND run_id = ?' if run_scoped else ''}",
            params,
        )
        counter: dict[str, int] = {}
        for row in flag_rows:
            for flag in (row["quality_flags"] or "").split(";"):
                if flag:
                    counter[flag] = counter.get(flag, 0) + 1
        report.by_flag = dict(
            sorted(counter.items(), key=lambda kv: kv[1], reverse=True)
        )

        # -- status breakdowns ------------------------------------------------
        report.by_match_status = await _count_by(store, "match_status", scope, params)
        report.by_tunnel_status = await _count_by(store, "tunnel_status", scope, params)

        # -- per-horizon --------------------------------------------------------
        horizon_rows = await store.aggregate(
            f"SELECT horizon_label, COUNT(*) AS rows_n, "
            f"SUM(CASE WHEN is_clean=1 THEN 1 ELSE 0 END) AS clean_n, "
            f"COUNT(DISTINCT CASE WHEN is_clean=1 "
            f"     THEN hotel_name_normalised || '|' || city END) AS hotels_n, "
            f"AVG(CASE WHEN is_clean=1 THEN price_difference END) AS avg_diff "
            f"FROM price_records {scope} GROUP BY horizon_label",
            params,
        )
        for row in horizon_rows:
            report.by_horizon[row["horizon_label"] or "?"] = {
                "rows": int(row["rows_n"] or 0),
                "clean": int(row["clean_n"] or 0),
                "distinct_hotels": int(row["hotels_n"] or 0),
                "avg_price_difference": _round(row["avg_diff"]),
            }

        # -- per-crossing (offset x stay-duration), summed across every city -
        # the second acceptance criterion: each crossing of the two analysis
        # variables must contain a minimum number of clean hotels ----------------
        crossing_rows = await store.aggregate(
            f"SELECT horizon_label, nights, COUNT(*) AS rows_n, "
            f"SUM(CASE WHEN is_clean=1 THEN 1 ELSE 0 END) AS clean_n, "
            f"COUNT(DISTINCT CASE WHEN is_clean=1 "
            f"     THEN hotel_name_normalised || '|' || city END) AS hotels_n "
            f"FROM price_records {scope} GROUP BY horizon_label, nights "
            f"ORDER BY horizon_label, nights",
            params,
        )
        for row in crossing_rows:
            label = f"{row['horizon_label'] or '?'}|{row['nights']}n"
            report.by_crossing[label] = {
                "horizon_label": row["horizon_label"] or "?",
                "nights": int(row["nights"] or 0),
                "rows": int(row["rows_n"] or 0),
                "clean": int(row["clean_n"] or 0),
                "distinct_hotels": int(row["hotels_n"] or 0),
            }

        # -- per-city -----------------------------------------------------------
        city_rows = await store.aggregate(
            f"SELECT city, COUNT(*) AS rows_n, "
            f"SUM(CASE WHEN is_clean=1 THEN 1 ELSE 0 END) AS clean_n, "
            f"COUNT(DISTINCT CASE WHEN is_clean=1 "
            f"     THEN hotel_name_normalised END) AS hotels_n "
            f"FROM price_records {scope} GROUP BY city "
            f"ORDER BY clean_n DESC LIMIT 60",
            params,
        )
        for row in city_rows:
            report.by_city[row["city"] or "?"] = {
                "rows": int(row["rows_n"] or 0),
                "clean": int(row["clean_n"] or 0),
                "distinct_hotels": int(row["hotels_n"] or 0),
            }

        # -- price statistics ----------------------------------------------------
        stats = await store.aggregate(
            f"SELECT AVG(nuitee_final_price) AS avg_nuitee, "
            f"AVG(booking_price) AS avg_booking, "
            f"MIN(nuitee_final_price) AS min_nuitee, "
            f"MAX(nuitee_final_price) AS max_nuitee, "
            f"AVG(nuitee_hidden_fees) AS avg_hidden_fees, "
            f"AVG(price_difference) AS avg_diff, "
            f"AVG(price_difference_pct) AS avg_diff_pct "
            f"FROM price_records WHERE is_clean = 1 "
            f"{'AND run_id = ?' if run_scoped else ''}",
            params,
        )
        if stats:
            row = stats[0]
            report.price_stats = {
                "avg_nuitee_final_price": _round(row["avg_nuitee"]),
                "avg_booking_price": _round(row["avg_booking"]),
                "min_nuitee_final_price": _round(row["min_nuitee"]),
                "max_nuitee_final_price": _round(row["max_nuitee"]),
                "avg_hidden_fees": _round(row["avg_hidden_fees"]),
                "avg_price_difference": _round(row["avg_diff"]),
                "avg_price_difference_pct": _round(row["avg_diff_pct"]),
            }

        # -- competitiveness ------------------------------------------------------
        report.competitiveness = await self._competitiveness(store, run_scoped, params)

        return report

    async def _competitiveness(
        self, store, run_scoped: bool, params: tuple
    ) -> dict[str, Any]:
        clause = "WHERE is_clean = 1" + (" AND run_id = ?" if run_scoped else "")

        rows = await store.aggregate(
            f"SELECT cheaper_platform, COUNT(*) AS n FROM price_records "
            f"{clause} GROUP BY cheaper_platform",
            params,
        )
        counts = {
            (row["cheaper_platform"] or "UNKNOWN"): int(row["n"] or 0) for row in rows
        }
        total = sum(counts.values()) or 1

        median_rows = await store.aggregate(
            f"SELECT price_difference FROM price_records "
            f"{clause} AND price_difference IS NOT NULL "
            f"ORDER BY price_difference",
            params,
        )
        values = [r["price_difference"] for r in median_rows]
        median = _median(values)
        mean = sum(values) / len(values) if values else None

        return {
            "nuitee_cheaper": counts.get("NUITEE", 0),
            "nuitee_cheaper_pct": round(counts.get("NUITEE", 0) / total * 100, 1),
            "booking_cheaper": counts.get("BOOKING", 0),
            "booking_cheaper_pct": round(counts.get("BOOKING", 0) / total * 100, 1),
            "tie": counts.get("TIE", 0),
            "mean_difference": _round(mean),
            "median_difference": _round(median),
            "sample_size": len(values),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _count_by(store, column: str, scope: str, params: tuple) -> dict[str, int]:
    rows = await store.aggregate(
        f"SELECT {column} AS k, COUNT(*) AS n FROM price_records {scope} "
        f"GROUP BY {column} ORDER BY n DESC",
        params,
    )
    return {(row["k"] or "UNKNOWN"): int(row["n"] or 0) for row in rows}


def _dates_valid(checkin: str, checkout: str) -> bool:
    try:
        start = datetime.strptime(checkin, "%Y-%m-%d").date()
        end = datetime.strptime(checkout, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return end > start


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _round(value: Any, digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
