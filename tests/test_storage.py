"""Tests for SQLite persistence, deduplication, resume, and reporting."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.config import Settings
from src.models import MatchStatus, PriceRecord, TunnelStatus
from src.pipeline.quality import QualityGate
from src.pipeline.storage import PriceStore
from src.utils.text import normalise_key


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.storage.output_dir = tmp_path
    s.storage.batch_size = 2
    return s


@pytest.fixture
async def store(settings: Settings) -> PriceStore:
    st = PriceStore(settings, run_id="test_run")
    await st.start_run({"test": True})
    return st


def make_record(name: str = "Hotel A", city: str = "Casablanca", **kw) -> PriceRecord:
    defaults = dict(
        hotel_name=name,
        hotel_name_normalised=normalise_key(name),
        city=city,
        checkin="2026-07-30",
        checkout="2026-07-31",
        horizon_label="J+3",
        horizon_offset_days=3,
        execution_date="2026-07-27",
        nuitee_final_price=1200.0,
        booking_price=1350.0,
        fuzzy_score=95.0,
        match_status=MatchStatus.OK.value,
        tunnel_status=TunnelStatus.OK.value,
        currency="MAD",
    )
    defaults.update(kw)
    return PriceRecord(**defaults).compute_derived()


class TestPersistence:
    async def test_add_and_flush(self, store: PriceStore) -> None:
        await store.add(make_record())
        await store.flush()
        assert await store.count() == 1

    async def test_autoflush_at_batch_size(self, store: PriceStore) -> None:
        await store.add(make_record("Hotel A"))
        await store.add(make_record("Hotel B"))   # batch_size=2 -> flush
        assert await store.count() == 2

    async def test_roundtrip_preserves_values(self, store: PriceStore) -> None:
        await store.add(make_record(nuitee_final_price=1234.56))
        await store.flush()
        row = (await store.fetch())[0]
        assert row["nuitee_final_price"] == pytest.approx(1234.56)
        assert row["run_id"] == "test_run"


class TestDeduplication:
    async def test_same_key_upserts(self, store: PriceStore, settings: Settings) -> None:
        gate = QualityGate(settings)
        await store.add(gate.evaluate(make_record(booking_price=1000.0)))
        await store.flush()
        await store.add(gate.evaluate(make_record(booking_price=2000.0)))
        await store.flush()

        assert await store.count() == 1
        assert (await store.fetch())[0]["booking_price"] == pytest.approx(2000.0)

    async def test_different_horizon_is_a_distinct_row(
        self, store: PriceStore
    ) -> None:
        await store.add(make_record(horizon_label="J+3"))
        await store.add(make_record(horizon_label="J+7"))
        await store.flush()
        assert await store.count() == 2

    async def test_dirty_row_never_overwrites_a_clean_one(
        self, store: PriceStore, settings: Settings
    ) -> None:
        """A failed re-scrape must not destroy good data."""
        gate = QualityGate(settings)

        good = gate.evaluate(make_record(booking_price=1350.0))
        assert good.is_clean
        await store.add(good)
        await store.flush()

        bad = gate.evaluate(make_record(booking_price=None))
        assert not bad.is_clean
        await store.add(bad)
        await store.flush()

        row = (await store.fetch())[0]
        assert row["booking_price"] == pytest.approx(1350.0)
        assert row["is_clean"] == 1


class TestResume:
    async def test_clean_rows_are_skippable(
        self, store: PriceStore, settings: Settings
    ) -> None:
        gate = QualityGate(settings)
        record = gate.evaluate(make_record())
        await store.add(record)
        await store.flush()
        assert store.already_done(record.dedup_key) is True

    async def test_dirty_rows_are_not_skipped(
        self, store: PriceStore, settings: Settings
    ) -> None:
        gate = QualityGate(settings)
        record = gate.evaluate(make_record(booking_price=None))
        await store.add(record)
        await store.flush()
        assert store.already_done(record.dedup_key) is False

    async def test_resume_disabled_skips_nothing(
        self, settings: Settings
    ) -> None:
        settings.storage.resume = False
        st = PriceStore(settings, run_id="no_resume")
        await st.start_run({})
        record = make_record()
        record.is_clean = True
        await st.add(record)
        await st.flush()
        assert st.already_done(record.dedup_key) is False


class TestIncrementalCsv:
    """
    The CSV must be visible and growing DURING a run, not written once at
    the very end - ``flush()`` (already called after every search window
    in the runner) rewrites it directly, without a separate export step.
    """

    async def test_flush_writes_csv_without_an_explicit_export(
        self, store: PriceStore, settings: Settings
    ) -> None:
        assert not settings.storage.csv_path.exists()

        await store.add(make_record("Hotel A"))
        await store.flush()

        assert settings.storage.csv_path.exists()
        assert "Hotel A" in settings.storage.csv_path.read_text(
            encoding="utf-8-sig"
        )

    async def test_each_flush_extends_the_csv(
        self, store: PriceStore, settings: Settings
    ) -> None:
        await store.add(make_record("Hotel A"))
        await store.flush()
        first_body = settings.storage.csv_path.read_text(encoding="utf-8-sig")
        assert "Hotel A" in first_body and "Hotel B" not in first_body

        await store.add(make_record("Hotel B", city="Rabat"))
        await store.flush()
        second_body = settings.storage.csv_path.read_text(encoding="utf-8-sig")
        assert "Hotel A" in second_body and "Hotel B" in second_body

    async def test_clean_csv_also_updates_on_flush(
        self, store: PriceStore, settings: Settings
    ) -> None:
        gate = QualityGate(settings)
        await store.add(gate.evaluate(make_record("Clean Hotel")))
        await store.flush()
        assert settings.storage.clean_csv_path.exists()
        assert "Clean Hotel" in settings.storage.clean_csv_path.read_text(
            encoding="utf-8-sig"
        )


class TestCsvBootstrapResume:
    """A fresh ``PriceStore`` (simulating a restarted process) must recover
    its resume/dedup state from ``prices.csv`` alone - there is no database
    file to read back from."""

    async def test_fresh_store_loads_prior_clean_rows_from_csv(
        self, settings: Settings
    ) -> None:
        gate = QualityGate(settings)

        first = PriceStore(settings, run_id="run_one")
        await first.start_run({})
        record = gate.evaluate(make_record("Hotel A"))
        await first.add(record)
        await first.flush()
        assert first.already_done(record.dedup_key) is True

        # A brand new store, as if the process had restarted - only the CSV
        # on disk carries the prior state forward.
        second = PriceStore(settings, run_id="run_two")
        await second.start_run({})

        assert second.already_done(record.dedup_key) is True
        assert await second.count() == 1

    async def test_fresh_store_ignores_dirty_rows_for_resume(
        self, settings: Settings
    ) -> None:
        gate = QualityGate(settings)

        first = PriceStore(settings, run_id="run_one")
        await first.start_run({})
        record = gate.evaluate(make_record("Hotel A", booking_price=None))
        assert not record.is_clean
        await first.add(record)
        await first.flush()

        second = PriceStore(settings, run_id="run_two")
        await second.start_run({})
        assert second.already_done(record.dedup_key) is False


class TestExport:
    async def test_csv_has_booking_url_last(
        self, store: PriceStore, settings: Settings
    ) -> None:
        await store.add(make_record(booking_url="https://booking.com/x"))
        path = await store.export_csv()
        header = path.read_text(encoding="utf-8-sig").splitlines()[0]
        assert header.split(",")[-1] == "Booking URL"

    async def test_clean_only_filters(
        self, store: PriceStore, settings: Settings
    ) -> None:
        gate = QualityGate(settings)
        await store.add(gate.evaluate(make_record("Clean Hotel")))
        await store.add(
            gate.evaluate(make_record("Dirty Hotel", booking_price=None))
        )
        await store.flush()

        clean_path = await store.export_csv(clean_only=True)
        body = clean_path.read_text(encoding="utf-8-sig")
        assert "Clean Hotel" in body
        assert "Dirty Hotel" not in body

    async def test_scraped_local_fees_round_trip_through_csv(
        self, store: PriceStore
    ) -> None:
        """
        ``nuitee_local_fees`` (the real "Local fees" figure scraped off the
        booking page) must survive add -> flush -> CSV export intact, with
        its own dedicated, clearly-labelled column - distinct from the older
        ``nuitee_hidden_fees`` estimate.
        """
        await store.add(
            make_record("Fee Hotel", nuitee_local_fees=145.25, nuitee_hidden_fees=145.25)
        )
        await store.flush()

        path = await store.export_csv()
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = list(csv.reader(handle))
        header, rows = reader[0], reader[1:]

        assert "Nuitee Local Fees (MAD, scraped)" in header
        local_fees_idx = header.index("Nuitee Local Fees (MAD, scraped)")

        row = next(r for r in rows if "Fee Hotel" in r)
        assert row[local_fees_idx] == "145.25"


class TestReport:
    async def test_headline_metric_counts_distinct_hotels(
        self, store: PriceStore, settings: Settings
    ) -> None:
        gate = QualityGate(settings)
        # Same hotel across two horizons = 2 rows but 1 distinct hotel.
        await store.add(gate.evaluate(make_record("Hotel A", horizon_label="J+3")))
        await store.add(gate.evaluate(make_record("Hotel A", horizon_label="J+7")))
        await store.add(gate.evaluate(make_record("Hotel B")))
        await store.flush()

        report = await gate.build_report(store)
        assert report.total_rows == 3
        assert report.clean_rows == 3
        assert report.distinct_clean_hotels == 2

    async def test_target_flag(self, store: PriceStore, settings: Settings) -> None:
        settings.quality.target_clean_hotels = 1
        gate = QualityGate(settings)
        await store.add(gate.evaluate(make_record()))
        await store.flush()

        report = await gate.build_report(store)
        assert report.target_met is True
        assert "MET" in report.render()

    async def test_rejection_reasons_are_counted(
        self, store: PriceStore, settings: Settings
    ) -> None:
        gate = QualityGate(settings)
        await store.add(gate.evaluate(make_record("A", booking_price=None)))
        await store.add(gate.evaluate(make_record("B", booking_price=None)))
        await store.flush()

        report = await gate.build_report(store)
        assert report.by_flag.get("MISSING_BOOKING_PRICE") == 2

    async def test_competitiveness_split(
        self, store: PriceStore, settings: Settings
    ) -> None:
        gate = QualityGate(settings)
        await store.add(
            gate.evaluate(
                make_record("A", nuitee_final_price=1000.0, booking_price=1200.0)
            )
        )
        await store.add(
            gate.evaluate(
                make_record("B", nuitee_final_price=1200.0, booking_price=1000.0)
            )
        )
        await store.flush()

        report = await gate.build_report(store)
        assert report.competitiveness["nuitee_cheaper"] == 1
        assert report.competitiveness["booking_cheaper"] == 1
