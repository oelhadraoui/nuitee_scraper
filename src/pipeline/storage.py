"""
src/pipeline/storage.py
=============================================================================
CSV-backed persistence, with an in-memory SQLite table as the query engine.

Why CSV is the store, not a file on disk
-----------------------------------------
Earlier versions of this pipeline wrote to a SQLite database file
(``hotel_prices.db``). That gave fast aggregate queries and safe concurrent
writes, but at two real costs users ran into in practice:

  * **Nothing was visible until the very end.** ``prices.csv`` was only
    written once, in ``PipelineRunner.run()``, after the entire plan (every
    city, every offset, every duration) had finished - on a multi-hour run
    there was no way to check progress or sanity-check early results without
    waiting for completion.
  * **The database file itself was a source of failures.** SQLite needs
    POSIX file locking, which some Docker bind mounts and network
    filesystems (NFS, SMB, certain fuse mounts) do not provide - a working
    scrape run would still fail at start-up with "disk I/O error" purely
    because of where the output directory happened to be mounted.

This version keeps the *fast query engine* (an in-memory SQLite table - the
crossing/report aggregates in ``quality.py`` and the resume/early-exit
lookups in ``runner.py`` are still plain SQL) but removes the *file on disk*:
the in-memory table is rebuilt from ``prices.csv`` at start-up (so resume and
report/export still work across restarts), and every ``flush()`` - which
``PipelineRunner`` already calls after each search window - rewrites
``prices.csv`` and ``prices_clean.csv`` from the table's current contents.
That means the CSV is never more than one window's worth of work out of
date, and there is no database file for a filesystem to reject.
"""

from __future__ import annotations

import asyncio
import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.config import Settings
from src.models import CSV_COLUMNS, CSV_HEADER_ALIASES, PriceRecord
from src.utils.logging_setup import get_logger
from src.utils.text import normalise_key

log = get_logger(__name__)

__all__ = ["PriceStore", "StorageUnavailableError"]


class StorageUnavailableError(RuntimeError):
    """The output directory cannot be written to."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_records (
    dedup_key             TEXT PRIMARY KEY,
    hotel_name            TEXT NOT NULL,
    hotel_name_normalised TEXT NOT NULL,
    city                  TEXT NOT NULL,
    checkin               TEXT NOT NULL,
    checkout              TEXT NOT NULL,
    nights                INTEGER DEFAULT 1,
    horizon_label         TEXT NOT NULL,
    horizon_offset_days   INTEGER DEFAULT 0,
    execution_date        TEXT NOT NULL,
    rooms                 INTEGER DEFAULT 1,
    adults_per_room       TEXT DEFAULT '2',

    nuitee_display_price  REAL,
    nuitee_final_price    REAL,
    nuitee_hidden_fees    REAL,
    nuitee_room_label     TEXT,
    tunnel_status         TEXT,
    tunnel_deepest_panel  TEXT,
    tunnel_panels_visited TEXT,
    nuitee_url            TEXT,

    booking_price         REAL,
    booking_matched_name  TEXT,
    fuzzy_score           REAL DEFAULT 0,
    match_status          TEXT,
    booking_url           TEXT,

    price_difference      REAL,
    price_difference_pct  REAL,
    cheaper_platform      TEXT,
    currency              TEXT DEFAULT 'MAD',

    is_clean              INTEGER DEFAULT 0,
    quality_flags         TEXT DEFAULT '',
    verified              INTEGER DEFAULT 0,
    scraped_at            TEXT,
    run_id                TEXT
);

CREATE INDEX IF NOT EXISTS idx_clean       ON price_records (is_clean);
CREATE INDEX IF NOT EXISTS idx_horizon     ON price_records (horizon_label);
CREATE INDEX IF NOT EXISTS idx_city        ON price_records (city);
CREATE INDEX IF NOT EXISTS idx_execution   ON price_records (execution_date);
CREATE INDEX IF NOT EXISTS idx_status      ON price_records (match_status, tunnel_status);
CREATE INDEX IF NOT EXISTS idx_run         ON price_records (run_id);
"""

_UPSERT_COLUMNS = [
    "dedup_key", "hotel_name", "hotel_name_normalised", "city", "checkin",
    "checkout", "nights", "horizon_label", "horizon_offset_days",
    "execution_date", "rooms", "adults_per_room", "nuitee_display_price",
    "nuitee_final_price", "nuitee_hidden_fees", "nuitee_room_label",
    "tunnel_status", "tunnel_deepest_panel", "tunnel_panels_visited",
    "nuitee_url", "booking_price", "booking_matched_name", "fuzzy_score",
    "match_status", "booking_url", "price_difference", "price_difference_pct",
    "cheaper_platform", "currency", "is_clean", "quality_flags", "verified",
    "scraped_at", "run_id",
]

_UPSERT_SQL = (
    f"INSERT INTO price_records ({', '.join(_UPSERT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _UPSERT_COLUMNS)}) "
    f"ON CONFLICT(dedup_key) DO UPDATE SET "
    + ", ".join(
        f"{col}=excluded.{col}"
        for col in _UPSERT_COLUMNS
        if col != "dedup_key"
    )
    # Never let a failed re-scrape overwrite a good row.
    + " WHERE excluded.is_clean >= price_records.is_clean"
)

#: Reverse of CSV_HEADER_ALIASES, for reading a friendly-header CSV back in.
_HEADER_TO_COLUMN = {alias: column for column, alias in CSV_HEADER_ALIASES.items()}


class PriceStore:
    """
    Async-friendly wrapper over an in-memory SQLite table of
    :class:`PriceRecord`, durably backed by ``prices.csv``.

    A single ``asyncio.Lock`` serialises every access to the in-memory
    connection (reads included, unlike the old per-call-connection design),
    since a single ``sqlite3`` connection is not safe to use concurrently
    from multiple worker threads.
    """

    def __init__(self, settings: Settings, run_id: Optional[str] = None) -> None:
        self.settings = settings
        self.csv_path: Path = settings.storage.csv_path
        self.clean_csv_path: Path = settings.storage.clean_csv_path
        self.run_id = run_id or datetime.utcnow().strftime("run_%Y%m%d_%H%M%S")
        self._lock = asyncio.Lock()
        self._buffer: list[PriceRecord] = []
        self._batch_size = settings.storage.batch_size
        self._done_keys: set[str] = set()
        self._conn: Optional[sqlite3.Connection] = None

    # ======================================================================
    # Lifecycle
    # ======================================================================

    def init_schema(self) -> None:
        """
        (Re)build the in-memory table and bootstrap it from ``prices.csv``
        if that file already exists - this is what makes resume and the
        ``report``/``export``/``doctor`` CLI commands work across process
        restarts without a database file on disk.
        """
        try:
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailableError(
                f"Cannot create the output directory {self.csv_path.parent} ({exc})."
            ) from exc

        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        loaded = self._bootstrap_from_csv()
        log.info(
            "Storage ready (in-memory table, backed by %s; run_id=%s%s)",
            self.csv_path, self.run_id,
            f"; loaded {loaded} existing row(s)" if loaded else "",
        )

    def _bootstrap_from_csv(self) -> int:
        """Load ``prices.csv`` (if present) into the fresh in-memory table."""
        assert self._conn is not None
        if not self.csv_path.is_file():
            return 0

        rows: list[tuple] = []
        try:
            with self.csv_path.open(encoding="utf-8-sig", newline="") as handle:
                for raw in csv.DictReader(handle):
                    row = _row_from_csv_dict(raw)
                    if row is not None:
                        rows.append(row)
        except (OSError, csv.Error) as exc:
            log.warning("Could not read existing %s (%s) - starting fresh", self.csv_path, exc)
            return 0

        if rows:
            self._conn.executemany(_UPSERT_SQL, rows)
            self._conn.commit()
        return len(rows)

    async def start_run(self, plan_summary: dict[str, Any]) -> None:
        """Initialise storage and warm the resume cache."""
        await asyncio.to_thread(self.init_schema)

        if self.settings.storage.resume:
            async with self._lock:
                self._done_keys = await asyncio.to_thread(self._load_done_keys)
            if self._done_keys:
                log.info(
                    "Resume enabled: %d clean rows already stored will be skipped",
                    len(self._done_keys),
                )

    def _load_done_keys(self) -> set[str]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT dedup_key FROM price_records WHERE is_clean = 1"
        ).fetchall()
        return {row["dedup_key"] for row in rows}

    async def finish_run(self) -> None:
        await self.flush()

    # ======================================================================
    # Writing
    # ======================================================================

    def already_done(self, record_key: str) -> bool:
        """True if this exact unit of work already produced a clean row."""
        return self.settings.storage.resume and record_key in self._done_keys

    async def add(self, record: PriceRecord) -> None:
        """Buffer one record; flushes automatically at ``batch_size``."""
        record.run_id = self.run_id
        async with self._lock:
            self._buffer.append(record)
            should_flush = len(self._buffer) >= self._batch_size
        if should_flush:
            await self.flush()

    async def add_many(self, records: Iterable[PriceRecord]) -> None:
        for record in records:
            await self.add(record)

    async def flush(self) -> int:
        """
        Write the buffer to the in-memory table AND rewrite both CSV files
        on disk, so ``prices.csv`` never lags more than one flush behind
        what has actually been scraped. Returns the number of rows written.
        """
        async with self._lock:
            pending, self._buffer = self._buffer, []
            if not pending:
                return 0

            rows = [self._to_row(record) for record in pending]

            def _write() -> None:
                assert self._conn is not None
                self._conn.executemany(_UPSERT_SQL, rows)
                self._conn.commit()
                self._write_csv(self.csv_path, clean_only=False)
                self._write_csv(self.clean_csv_path, clean_only=True)

            await asyncio.to_thread(_write)

        for record in pending:
            if record.is_clean:
                self._done_keys.add(record.dedup_key)

        log.debug("flushed %d record(s); CSV files updated", len(rows))
        return len(rows)

    @staticmethod
    def _to_row(record: PriceRecord) -> tuple:
        data = record.to_dict()
        data["is_clean"] = int(bool(record.is_clean))
        data["verified"] = int(bool(record.verified))
        return tuple(data.get(column) for column in _UPSERT_COLUMNS)

    # ======================================================================
    # Reading
    # ======================================================================

    async def count(self, where: str = "", params: tuple = ()) -> int:
        def _count() -> int:
            assert self._conn is not None
            clause = f" WHERE {where}" if where else ""
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM price_records{clause}", params
            ).fetchone()
            return int(row["n"] or 0)

        async with self._lock:
            return await asyncio.to_thread(_count)

    async def clean_count(self, run_scoped: bool = False) -> int:
        if run_scoped:
            return await self.count("is_clean = 1 AND run_id = ?", (self.run_id,))
        return await self.count("is_clean = 1")

    async def fetch(
        self,
        where: str = "",
        params: tuple = (),
        order_by: str = "city, horizon_label, execution_date",
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        def _fetch() -> list[dict[str, Any]]:
            assert self._conn is not None
            clause = f" WHERE {where}" if where else ""
            order = f" ORDER BY {order_by}" if order_by else ""
            bound = f" LIMIT {int(limit)}" if limit else ""
            rows = self._conn.execute(
                f"SELECT * FROM price_records{clause}{order}{bound}", params
            ).fetchall()
            return [dict(row) for row in rows]

        async with self._lock:
            return await asyncio.to_thread(_fetch)

    async def aggregate(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Run an arbitrary read-only aggregate (used by the quality report)."""
        def _run() -> list[dict[str, Any]]:
            assert self._conn is not None
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

        async with self._lock:
            return await asyncio.to_thread(_run)

    # ======================================================================
    # Export
    # ======================================================================

    async def export_csv(
        self,
        path: Optional[Path] = None,
        *,
        clean_only: bool = False,
        friendly_headers: bool = True,
        run_scoped: bool = False,
    ) -> Path:
        """
        Export to CSV with ``Booking URL`` last, matching the original
        deliverable's column convention.

        With every default argument this reproduces exactly what ``flush()``
        already writes after each window; the explicit path/run_scoped/
        raw-header options exist for the ``export`` CLI command.
        """
        await self.flush()

        target = path or (self.clean_csv_path if clean_only else self.csv_path)

        async with self._lock:
            count = await asyncio.to_thread(
                self._write_csv, target, clean_only, friendly_headers, run_scoped
            )
        log.info("Exported %d row(s) to %s", count, target)
        return target

    def _write_csv(
        self,
        target: Path,
        clean_only: bool,
        friendly_headers: bool = True,
        run_scoped: bool = False,
    ) -> int:
        """Write ``target`` from the in-memory table's current contents."""
        assert self._conn is not None
        target.parent.mkdir(parents=True, exist_ok=True)

        conditions, params = [], []
        if clean_only:
            conditions.append("is_clean = 1")
        if run_scoped:
            conditions.append("run_id = ?")
            params.append(self.run_id)
        where = " AND ".join(conditions)
        clause = f" WHERE {where}" if where else ""

        rows = [
            dict(row)
            for row in self._conn.execute(
                f"SELECT * FROM price_records{clause} "
                f"ORDER BY city, horizon_label, execution_date",
                tuple(params),
            ).fetchall()
        ]

        header = (
            [CSV_HEADER_ALIASES.get(c, c) for c in CSV_COLUMNS]
            if friendly_headers
            else list(CSV_COLUMNS)
        )
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for row in rows:
                writer.writerow([_csv_value(row.get(c)) for c in CSV_COLUMNS])

        return len(rows)

    async def export_parquet(self, path: Optional[Path] = None) -> Optional[Path]:
        """Export to Parquet when pandas + pyarrow are available."""
        try:
            import pandas as pd
        except ImportError:
            log.warning("pandas not installed - skipping Parquet export")
            return None

        await self.flush()
        target = path or (self.settings.storage.output_dir / "prices.parquet")
        rows = await self.fetch()
        if not rows:
            return None

        def _write() -> None:
            pd.DataFrame(rows).to_parquet(target, index=False)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            log.warning("Parquet export failed (%s) - is pyarrow installed?", exc)
            return None

        log.info("Exported %d row(s) to %s", len(rows), target)
        return target


def _csv_value(value: Any) -> Any:
    """Render a stored value for CSV: booleans as true/false, None as empty."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _row_from_csv_dict(raw: dict[str, str]) -> Optional[tuple]:
    """
    Convert one ``csv.DictReader`` row (friendly headers) back into a tuple
    matching ``_UPSERT_COLUMNS``, for bootstrapping the in-memory table.

    Fields that were deliberately left out of the trimmed CSV export
    (``horizon_offset_days``, ``tunnel_deepest_panel``, ``tunnel_panels_visited``,
    ``run_id``, ``verified``) come back as defaults - they're diagnostic-only
    and are not needed for resume, reporting, or re-export.
    """
    row = {_HEADER_TO_COLUMN.get(k, k): v for k, v in raw.items()}
    hotel_name = (row.get("hotel_name") or "").strip()
    city = (row.get("city") or "").strip()
    checkin = (row.get("checkin") or "").strip()
    checkout = (row.get("checkout") or "").strip()
    horizon_label = (row.get("horizon_label") or "").strip()
    if not (hotel_name and city and checkin and checkout):
        return None

    hotel_name_normalised = normalise_key(hotel_name)
    rooms = _as_int(row.get("rooms"), default=1)
    adults_per_room = (row.get("adults_per_room") or "2").strip()

    dedup_key = _dedup_key(
        hotel_name_normalised, city, checkin, checkout,
        horizon_label, rooms, adults_per_room,
    )

    values = {
        "dedup_key": dedup_key,
        "hotel_name": hotel_name,
        "hotel_name_normalised": hotel_name_normalised,
        "city": city,
        "checkin": checkin,
        "checkout": checkout,
        "nights": _as_int(row.get("nights"), default=1),
        "horizon_label": horizon_label,
        "horizon_offset_days": 0,
        "execution_date": checkin,
        "rooms": rooms,
        "adults_per_room": adults_per_room,
        "nuitee_display_price": _as_float(row.get("nuitee_display_price")),
        "nuitee_final_price": _as_float(row.get("nuitee_final_price")),
        "nuitee_hidden_fees": _as_float(row.get("nuitee_hidden_fees")),
        "nuitee_room_label": row.get("nuitee_room_label") or None,
        "tunnel_status": row.get("tunnel_status") or None,
        "tunnel_deepest_panel": "unknown",
        "tunnel_panels_visited": "",
        "nuitee_url": row.get("nuitee_url") or None,
        "booking_price": _as_float(row.get("booking_price")),
        "booking_matched_name": row.get("booking_matched_name") or None,
        "fuzzy_score": _as_float(row.get("fuzzy_score")) or 0.0,
        "match_status": row.get("match_status") or None,
        "booking_url": row.get("booking_url") or None,
        "price_difference": _as_float(row.get("price_difference")),
        "price_difference_pct": _as_float(row.get("price_difference_pct")),
        "cheaper_platform": row.get("cheaper_platform") or None,
        "currency": row.get("currency") or "MAD",
        "is_clean": 1 if str(row.get("is_clean", "")).strip().lower() in
        ("1", "true", "yes") else 0,
        "quality_flags": row.get("quality_flags") or "",
        "verified": 0,
        "scraped_at": row.get("scraped_at") or "",
        "run_id": "",
    }
    return tuple(values.get(column) for column in _UPSERT_COLUMNS)


def _dedup_key(
    hotel_name_normalised: str,
    city: str,
    checkin: str,
    checkout: str,
    horizon_label: str,
    rooms: int,
    adults_per_room: str,
) -> str:
    """Mirrors ``PriceRecord.dedup_key`` - must stay in sync with models.py."""
    import hashlib

    raw = "|".join(
        [
            hotel_name_normalised,
            city.lower(),
            checkin,
            checkout,
            horizon_label,
            str(rooms),
            adults_per_room,
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
