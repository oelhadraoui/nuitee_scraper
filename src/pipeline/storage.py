"""
src/pipeline/storage.py
=============================================================================
SQLite-backed persistence with CSV/Parquet export.

Why SQLite and not a bare CSV
-----------------------------
The finalised run is 1,000+ clean hotels x 2 horizons x a rolling 2-3 month
window - six figures of rows written by concurrent workers over many hours.
A flat appended CSV gives you no deduplication, no crash recovery, no resume,
and interleaved writes from concurrent tasks. SQLite gives all four for free:

  * ``dedup_key`` is the PRIMARY KEY, so re-scraping the same
    (hotel, city, dates, horizon) UPSERTs instead of duplicating.
  * ``already_done()`` lets an interrupted run skip completed work on restart.
  * WAL mode plus a single writer task keeps concurrent writes safe.
  * The quality report is a handful of SQL aggregates rather than a full
    pandas load of a 100 MB file.

CSV remains the client-facing deliverable and is exported on demand.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from src.config import Settings
from src.models import CSV_COLUMNS, CSV_HEADER_ALIASES, PriceRecord
from src.utils.logging_setup import get_logger

log = get_logger(__name__)

__all__ = ["PriceStore", "StorageUnavailableError"]


class StorageUnavailableError(RuntimeError):
    """The database cannot be opened on this filesystem."""


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
    adults_per_room       TEXT DEFAULT '[2]',

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

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT,
    finished_at  TEXT,
    plan_summary TEXT,
    settings     TEXT,
    row_count    INTEGER DEFAULT 0,
    clean_count  INTEGER DEFAULT 0
);
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


class PriceStore:
    """
    Async-friendly wrapper over a SQLite database of :class:`PriceRecord`.

    All SQLite calls run in a thread executor, so the event loop is never
    blocked by disk I/O. A single ``asyncio.Lock`` serialises writers, which
    is what makes concurrent scrape tasks safe.
    """

    def __init__(self, settings: Settings, run_id: Optional[str] = None) -> None:
        self.settings = settings
        self.path: Path = settings.storage.database_path
        self.run_id = run_id or datetime.utcnow().strftime("run_%Y%m%d_%H%M%S")
        self._lock = asyncio.Lock()
        self._buffer: list[PriceRecord] = []
        self._batch_size = settings.storage.batch_size
        self._done_keys: set[str] = set()
        self._journal_mode: Optional[str] = None

    # ======================================================================
    # Lifecycle
    # ======================================================================

    def connect(self) -> sqlite3.Connection:
        """
        A tuned connection. Callers must close it.

        WAL is preferred (concurrent readers during a long write-heavy run),
        but it requires shared-memory support that some Docker bind mounts and
        network filesystems do not provide - there, `PRAGMA journal_mode=WAL`
        raises "disk I/O error". We probe once and fall back to the portable
        DELETE journal, which works everywhere at a modest cost in throughput.
        """
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")

        if self._journal_mode is None:
            self._journal_mode = self._probe_journal_mode(connection)

        try:
            connection.execute(f"PRAGMA journal_mode={self._journal_mode}")
            connection.execute(
                "PRAGMA synchronous="
                + ("NORMAL" if self._journal_mode == "WAL" else "FULL")
            )
        except sqlite3.Error:
            pass

        return connection

    @staticmethod
    def _probe_journal_mode(connection: sqlite3.Connection) -> str:
        """Return 'WAL' when the filesystem supports it, else 'DELETE'."""
        try:
            row = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = (row[0] if row else "").upper()
            if mode == "WAL":
                return "WAL"
        except sqlite3.Error as exc:
            log.warning(
                "WAL journal unavailable on this filesystem (%s) - "
                "falling back to the DELETE journal.", exc,
            )
        return "DELETE"

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(self.connect()) as connection:
                connection.executescript(_SCHEMA)
                connection.commit()
        except sqlite3.OperationalError as exc:
            # Some network filesystems (NFS, SMB, certain fuse mounts used by
            # Docker Desktop on macOS/Windows) do not implement the POSIX
            # locking SQLite needs. The error text alone is useless, so make
            # the remedy explicit.
            raise StorageUnavailableError(
                f"SQLite cannot operate on {self.path} ({exc}).\n"
                f"This filesystem likely does not support file locking.\n"
                f"Fix: point the database at a local volume, e.g.\n"
                f"  STORAGE_OUTPUT_DIR=/var/lib/nuitee-scraper\n"
                f"or use a named Docker volume instead of a bind mount."
            ) from exc
        log.info(
            "Storage ready at %s (run_id=%s, journal=%s)",
            self.path, self.run_id, self._journal_mode,
        )

    async def start_run(self, plan_summary: dict[str, Any]) -> None:
        """Register this run and warm the resume cache."""
        await asyncio.to_thread(self.init_schema)

        def _insert() -> None:
            with closing(self.connect()) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO runs "
                    "(run_id, started_at, plan_summary, settings) VALUES (?,?,?,?)",
                    (
                        self.run_id,
                        datetime.utcnow().isoformat(),
                        json.dumps(plan_summary, default=str),
                        json.dumps(
                            {
                                "horizon": self.settings.horizon.model_dump(),
                                "quality": self.settings.quality.model_dump(),
                                "tunnel_enabled": self.settings.tunnel.enabled,
                            },
                            default=str,
                        ),
                    ),
                )
                connection.commit()

        await asyncio.to_thread(_insert)

        if self.settings.storage.resume:
            self._done_keys = await asyncio.to_thread(self._load_done_keys)
            if self._done_keys:
                log.info(
                    "Resume enabled: %d clean rows already stored will be skipped",
                    len(self._done_keys),
                )

    def _load_done_keys(self) -> set[str]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT dedup_key FROM price_records WHERE is_clean = 1"
            ).fetchall()
        return {row["dedup_key"] for row in rows}

    async def finish_run(self) -> None:
        await self.flush()

        def _update() -> None:
            with closing(self.connect()) as connection:
                totals = connection.execute(
                    "SELECT COUNT(*) AS n, "
                    "SUM(CASE WHEN is_clean=1 THEN 1 ELSE 0 END) AS clean "
                    "FROM price_records WHERE run_id = ?",
                    (self.run_id,),
                ).fetchone()
                connection.execute(
                    "UPDATE runs SET finished_at=?, row_count=?, clean_count=? "
                    "WHERE run_id=?",
                    (
                        datetime.utcnow().isoformat(),
                        totals["n"] or 0,
                        totals["clean"] or 0,
                        self.run_id,
                    ),
                )
                connection.commit()

        await asyncio.to_thread(_update)

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
        """Write the buffer to disk. Returns the number of rows written."""
        async with self._lock:
            pending, self._buffer = self._buffer, []
        if not pending:
            return 0

        rows = [self._to_row(record) for record in pending]

        def _write() -> None:
            with closing(self.connect()) as connection:
                connection.executemany(_UPSERT_SQL, rows)
                connection.commit()

        await asyncio.to_thread(_write)

        for record in pending:
            if record.is_clean:
                self._done_keys.add(record.dedup_key)

        log.debug("flushed %d record(s)", len(rows))
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
            clause = f" WHERE {where}" if where else ""
            with closing(self.connect()) as connection:
                row = connection.execute(
                    f"SELECT COUNT(*) AS n FROM price_records{clause}", params
                ).fetchone()
            return int(row["n"] or 0)

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
            clause = f" WHERE {where}" if where else ""
            order = f" ORDER BY {order_by}" if order_by else ""
            bound = f" LIMIT {int(limit)}" if limit else ""
            with closing(self.connect()) as connection:
                rows = connection.execute(
                    f"SELECT * FROM price_records{clause}{order}{bound}", params
                ).fetchall()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_fetch)

    async def aggregate(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Run an arbitrary read-only aggregate (used by the quality report)."""
        def _run() -> list[dict[str, Any]]:
            with closing(self.connect()) as connection:
                return [dict(row) for row in connection.execute(sql, params).fetchall()]

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
        """
        await self.flush()

        target = path or (
            self.settings.storage.clean_csv_path
            if clean_only
            else self.settings.storage.csv_path
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        conditions, params = [], []
        if clean_only:
            conditions.append("is_clean = 1")
        if run_scoped:
            conditions.append("run_id = ?")
            params.append(self.run_id)
        where = " AND ".join(conditions)

        rows = await self.fetch(where=where, params=tuple(params))

        def _write() -> None:
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

        await asyncio.to_thread(_write)
        log.info("Exported %d row(s) to %s", len(rows), target)
        return target

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
    """Render a DB value for CSV: booleans as true/false, None as empty."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
