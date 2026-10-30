"""Migrate owned recorder units using the durable, unchanged m³ source ledger."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import math
import unittest
from unittest.mock import patch

from homeassistant.components.recorder import statistics as ha_statistics
from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.const import UnitOfVolume
from homeassistant.helpers.update_coordinator import UpdateFailed
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query
from test_aigues_barcelona_history import DAILY_ID, HOURLY_ID, MADRID, FakeStore, reading
import test_history_energy_runtime as energy_fixture
import test_history_interval_migration as interval_fixture

from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.statistics import (
    Frequency,
    build_statistics,
    normalize_readings,
)

ENERGY_ID = energy_fixture.ENERGY_ID


class LitresRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = energy_fixture.EnergyRecorderTests()
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.setUp()
        self.coordinator = self.runtime.coordinator
        self.fixture = self.runtime.fixture
        self.runtime.configure(self.coordinator, daily_fallback=False)

    def raw_rows(self, statistic_id):
        """Inspect physical SQL values without the shared fixture's unit normalization."""
        with self.fixture.sessions() as session:
            return [
                tuple(row)
                for row in session.execute(
                    select(Statistics.start_ts, Statistics.state, Statistics.sum)
                    .join(StatisticsMeta, Statistics.metadata_id == StatisticsMeta.id)
                    .where(StatisticsMeta.statistic_id == statistic_id)
                    .order_by(Statistics.start_ts)
                )
            ]

    def metadata(self):
        return ha_statistics.get_metadata(
            self.coordinator.hass, statistic_ids={DAILY_ID, HOURLY_ID, ENERGY_ID}
        )

    def owned_rows(self):
        return {sid: self.raw_rows(sid) for sid in (DAILY_ID, HOURLY_ID, ENERGY_ID)}

    async def seed_cubic_metres(self, *, zero=False, empty=False):
        first, detailed = date(2026, 6, 1), date(2026, 6, 3)
        daily = (
            []
            if empty
            else [
                reading(f"{first}T23:02:00+01:00", 0 if zero else 100.012, 0 if zero else 0.012),
                reading(f"{detailed}T23:02:00+01:00", 0 if zero else 100.036, 0 if zero else 0.024),
            ]
        )
        hourly = (
            []
            if empty
            else energy_fixture.provider_hours(
                detailed,
                [0 if zero else 1] * 24,
                counter_before="0" if zero else "100.012",
            )
        )
        ledger = {
            frequency.value: normalize_readings(rows, frequency, MADRID)
            for frequency, rows in ((Frequency.DAILY, daily), (Frequency.HOURLY, hourly))
        }
        await self.coordinator._save_ledger(ledger)
        self.coordinator._ledger = deepcopy(ledger)
        energy, _ = self.coordinator._energy_projection()
        points = {
            Frequency.DAILY: build_statistics(ledger["DAILY"]),
            Frequency.HOURLY: build_statistics(ledger["HOURLY"]),
            sensor.ENERGY_SERIES: build_statistics(energy),
        }
        for series, values in points.items():
            metadata = {
                **self.coordinator._statistic_metadata(series),
                "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
            }
            self.assertTrue(
                ha_statistics.import_statistics(self.fixture.queue, metadata, values, Statistics)
            )
        self.fixture.queue.async_import_statistics.reset_mock()
        self.fixture.queue.async_clear_statistics.reset_mock()
        return deepcopy(self.coordinator._store.saved)

    def assert_litres(self):
        for _, metadata in self.metadata().values():
            self.assertEqual(metadata["unit_of_measurement"], UnitOfVolume.LITERS)
            self.assertEqual(metadata["unit_class"], "volume")
            self.assertEqual(metadata["source"], "aigues_barcelona_history")

    async def test_all_three_owned_series_migrate_values_and_metadata_without_clearing(self):
        source = await self.seed_cubic_metres()
        old = self.owned_rows()
        old_metadata = self.metadata()
        await self.coordinator._publish()

        self.assert_litres()
        for sid, rows in old.items():
            migrated = self.raw_rows(sid)
            self.assertEqual([row[0] for row in migrated], [row[0] for row in rows])
            self.assertEqual(self.metadata()[sid][0], old_metadata[sid][0])
            for previous, current in zip(rows, migrated, strict=True):
                self.assertAlmostEqual(current[1], previous[1] * 1000)
                self.assertAlmostEqual(current[2], previous[2] * 1000)
        self.assertEqual(self.raw_rows(DAILY_ID)[-1][2], 36)
        self.assertEqual(self.raw_rows(HOURLY_ID)[-1][2], 24)
        self.assertEqual(self.raw_rows(ENERGY_ID)[-1][2], 36)
        self.assertEqual(self.coordinator._store.saved, source)
        self.assertEqual(self.coordinator._ledger["DAILY"], source["DAILY"])
        self.assertEqual(self.coordinator._ledger["HOURLY"], source["HOURLY"])
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.runtime.assert_native_preserved()

    async def test_reload_and_repeat_do_not_scale_litres_again_or_rewrite_source(self):
        source = await self.seed_cubic_metres()
        await self.coordinator._publish()
        migrated, metadata = self.owned_rows(), self.metadata()
        self.fixture.queue.async_import_statistics.reset_mock()

        await self.coordinator._publish()
        self.runtime.restart(daily_fallback=False)
        self.coordinator = self.runtime.coordinator
        await self.coordinator._load_ledger()
        await self.coordinator._publish()

        self.assertEqual(self.owned_rows(), migrated)
        self.assertEqual(self.metadata(), metadata)
        self.assertEqual(self.coordinator._store.saved, source)
        self.coordinator._store.async_save.assert_not_awaited()
        self.fixture.queue.async_import_statistics.assert_not_called()
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.runtime.assert_native_preserved()

    async def test_provider_corrections_after_migration_keep_litres_and_raw_m3(self):
        await self.seed_cubic_metres()
        await self.coordinator._publish()
        first, detailed = date(2026, 6, 1), date(2026, 6, 3)
        self.runtime.set_provider_response([reading(f"{first}T23:02:00+01:00", 100.015, 0.015)], [])
        await self.coordinator._import_range(first, first + timedelta(days=1), (Frequency.DAILY,))
        self.assertAlmostEqual(self.raw_rows(DAILY_ID)[-1][2], 39)
        self.assertAlmostEqual(self.raw_rows(ENERGY_ID)[-1][2], 39)
        self.runtime.set_provider_response(
            [], [reading(f"{detailed}T00:02:00+01:00", 100.014, 0.002)]
        )
        await self.coordinator._import_ranges(
            {Frequency.HOURLY: (detailed, detailed + timedelta(days=1))}, provider_dates=True
        )
        self.assertAlmostEqual(self.raw_rows(HOURLY_ID)[-1][2], 25)
        self.assertAlmostEqual(self.raw_rows(ENERGY_ID)[-1][2], 40)
        self.assertEqual(next(iter(self.coordinator._ledger["DAILY"].values()))["delta"], 0.015)
        self.assertEqual(next(iter(self.coordinator._ledger["HOURLY"].values()))["delta"], 0.002)
        self.assert_litres()
        self.runtime.assert_native_preserved()

    async def test_zero_values_still_republish_the_unit_metadata(self):
        source = await self.seed_cubic_metres(zero=True)
        before = self.owned_rows()
        await self.coordinator._publish()
        self.assertEqual(self.owned_rows(), before)
        self.assert_litres()
        self.assertEqual(self.fixture.queue.async_import_statistics.call_count, 3)
        self.assertEqual(self.coordinator._store.saved, source)
        self.fixture.queue.async_clear_statistics.assert_not_called()

    async def test_empty_existing_series_update_metadata_without_inventing_a_row(self):
        source = await self.seed_cubic_metres(empty=True)
        await self.coordinator._publish()
        self.assert_litres()
        self.assertEqual(self.owned_rows(), {DAILY_ID: [], HOURLY_ID: [], ENERGY_ID: []})
        self.assertEqual(self.fixture.queue.async_import_statistics.call_count, 3)
        self.assertEqual(self.coordinator._store.saved, source)
        self.fixture.queue.async_clear_statistics.assert_not_called()

    async def test_unknown_units_class_or_ownership_refuse_before_any_publication(self):
        source = await self.seed_cubic_metres()
        before = self.owned_rows()
        original = self.metadata()[ENERGY_ID][1]
        for invalid in (
            {"unit_of_measurement": "gal"},
            {"unit_of_measurement": None},
            {"unit_class": None},
            {"source": "recorder"},
        ):
            with self.subTest(invalid=invalid):
                ha_statistics.import_statistics(
                    self.fixture.queue, {**original, **invalid}, [], Statistics
                )
                if "source" in invalid:
                    # HA correctly keeps ownership immutable on normal imports.
                    # Simulate pre-existing foreign metadata only in this isolated DB.
                    with self.fixture.sessions() as session:
                        metadata = session.scalars(
                            select(StatisticsMeta).where(StatisticsMeta.statistic_id == ENERGY_ID)
                        ).one()
                        metadata.source = invalid["source"]
                        session.commit()
                    self.fixture.queue.statistics_meta_manager._clear_cache([ENERGY_ID])
                with self.assertRaisesRegex(ValueError, "ownership or volume units"):
                    await self.coordinator._publish()
                self.assertEqual(self.owned_rows(), before)
                self.assertEqual(self.coordinator._store.saved, source)
                self.fixture.queue.async_import_statistics.assert_not_called()
                self.fixture.queue.async_clear_statistics.assert_not_called()
        self.runtime.assert_native_preserved()

    async def test_true_m3_legacy_hourly_source_shifts_once_and_publishes_litres(self):
        legacy = interval_fixture.legacy_source()
        for frequency in Frequency:
            ha_statistics.import_statistics(
                self.fixture.queue,
                {
                    **self.coordinator._statistic_metadata(frequency),
                    "unit_of_measurement": UnitOfVolume.CUBIC_METERS,
                },
                build_statistics(legacy[frequency.value]),
                Statistics,
            )
        old = self.owned_rows()
        self.coordinator._store = FakeStore(legacy)
        self.coordinator._ledger = None
        await self.coordinator._load_ledger()
        await self.coordinator._publish()

        self.assert_litres()
        for sid in (DAILY_ID, HOURLY_ID):
            for before, after in zip(old[sid], self.raw_rows(sid), strict=True):
                self.assertEqual(after[0], before[0] - (3600 if sid == HOURLY_ID else 0))
                self.assertAlmostEqual(after[1], before[1] * 1000)
                self.assertAlmostEqual(after[2], before[2] * 1000)
        saved = self.coordinator._store.saved
        self.assertEqual(saved["hourly_interval_version"], 2)
        self.assertFalse(saved["hourly_replay_pending"])
        self.assertEqual(saved["DAILY"], legacy["DAILY"])
        for key, old_row in legacy["HOURLY"].items():
            row = next(
                r for r in saved["HOURLY"].values() if r["observed_at"] == old_row["observed_at"]
            )
            self.assertEqual((row["state"], row["delta"]), (old_row["state"], old_row["delta"]))
        self.fixture.queue.async_clear_statistics.assert_called_once_with([HOURLY_ID])
        self.runtime.assert_native_preserved()

    async def test_dropped_metadata_change_is_detected_then_retried(self):
        await self.seed_cubic_metres()
        actual_import = self.fixture.queue.async_import_statistics.side_effect

        def keep_old_unit(metadata, points, table):
            return actual_import(
                {**metadata, "unit_of_measurement": UnitOfVolume.CUBIC_METERS}, points, table
            )

        self.fixture.queue.async_import_statistics.side_effect = keep_old_unit
        with self.assertRaisesRegex(UpdateFailed, "series metadata"):
            await self.coordinator._publish()
        self.fixture.queue.async_import_statistics.side_effect = actual_import
        await self.coordinator._publish()
        self.assert_litres()
        self.assertEqual(self.raw_rows(ENERGY_ID)[-1][2], 36)
        self.runtime.assert_native_preserved()

    async def test_swallowed_core_row_error_is_detected_and_full_source_recovers(self):
        source = await self.seed_cubic_metres()
        before = self.owned_rows()
        original_update = Query.update

        def fail_statistic_rows(query, *args, **kwargs):
            if query.column_descriptions[0].get("entity") is Statistics:
                raise SQLAlchemyError("Injected statistic-row write failure")
            return original_update(query, *args, **kwargs)

        with (
            patch.object(Query, "update", fail_statistic_rows),
            self.assertLogs("homeassistant.components.recorder.statistics", level="ERROR"),
            self.assertRaisesRegex(UpdateFailed, "complete History batch"),
        ):
            await self.coordinator._publish()
        self.assertEqual(self.owned_rows(), before)
        self.assertEqual(self.coordinator._store.saved, source)
        await self.coordinator._publish()
        self.assert_litres()
        self.assertEqual(self.raw_rows(ENERGY_ID)[-1][2], 36)
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.runtime.assert_native_preserved()

    def test_litre_overflow_is_rejected_without_changing_source(self):
        source = normalize_readings(
            [reading("2026-06-01T23:02:00+01:00", 1e307, 0.001)], Frequency.DAILY, MADRID
        )
        before = deepcopy(source)
        self.assertTrue(math.isfinite(build_statistics(source)[0]["state"]))
        with self.assertRaisesRegex(ValueError, "litre statistics must remain finite"):
            sensor._published_statistics(source)
        self.assertEqual(source, before)
