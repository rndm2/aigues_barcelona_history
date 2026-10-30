"""Verify mixed-resolution Energy publication against the actual HA recorder."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from homeassistant.components.recorder import statistics as ha_statistics
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_aigues_barcelona_history import (
    DAILY_ID,
    HOURLY_ID,
    MADRID,
    FakeStore,
    make_coordinator,
    reading,
)
import test_history_integrity as recorder_fixture

from custom_components.aigues_barcelona_history.const import CONF_ENERGY_DAILY_FALLBACK
from custom_components.aigues_barcelona_history.statistics import Frequency, normalize_readings

ENERGY_ID = "aigues_barcelona_history:test123_energy_consumption"
UTC = timezone.utc
CET = timezone(timedelta(hours=1))


def provider_hours(day, deltas_l, *, counter_before="100.000"):
    """Construct each meter reading from independent physical increments."""
    counter = Decimal(counter_before)
    output = []
    for hour, delta_l in enumerate(deltas_l):
        delta = Decimal(delta_l) / 1000
        counter += delta
        stamp = datetime.combine(day, time(hour, 2), tzinfo=CET)
        output.append(reading(stamp.isoformat(), float(counter), float(delta)))
    return output


class EnergyRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = recorder_fixture.RecorderSQLiteIntegrityTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        with self.fixture.engine.connect() as connection:
            # HA enables metadata cascades in its actual SQLite connection.
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        self.coordinator = self.fixture.coordinator
        self.configure(self.coordinator)
        self.fixture.queue.async_clear_statistics = Mock(side_effect=self.clear_statistics)

    @staticmethod
    def configure(coordinator, *, daily_fallback=True):
        coordinator.energy_statistic_id = ENERGY_ID
        coordinator._entry().options[CONF_ENERGY_DAILY_FALLBACK] = daily_fallback
        if not hasattr(coordinator.hass, "states"):
            coordinator.hass.states = SimpleNamespace(get=lambda _: None)

    def clear_statistics(self, ids):
        ha_statistics.clear_statistics(self.fixture.queue, ids)

    def source_rows(self):
        return {
            statistic_id: self.fixture.database_rows(statistic_id)
            for statistic_id in (DAILY_ID, HOURLY_ID, self.fixture.old_id)
        }

    def assert_native_preserved(self):
        self.assertEqual(self.fixture.database_rows(self.fixture.old_id), self.fixture.old_snapshot)

    def recorder_period(self, first_day, stop_day, period):
        start = datetime.combine(first_day, time.min, tzinfo=MADRID).astimezone(UTC)
        stop = datetime.combine(stop_day, time.min, tzinfo=MADRID).astimezone(UTC)
        rows = ha_statistics.statistics_during_period(
            self.coordinator.hass,
            start,
            stop - timedelta(microseconds=1),
            {ENERGY_ID},
            period,
            {"volume": "L"},
            {"state", "sum", "change"},
        ).get(ENERGY_ID, [])
        return [row for row in rows if start.timestamp() <= row["start"] < stop.timestamp()]

    async def import_rows(self, daily, hourly, first_day, stop_day, frequencies=tuple(Frequency)):
        self.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else hourly
        )
        await self.coordinator._import_range(first_day, stop_day, frequencies)

    def restart(self, *, daily_fallback=True):
        old = self.coordinator
        self.coordinator = make_coordinator(FakeStore(old._store.saved))
        self.coordinator.hass = old.hass
        self.configure(self.coordinator, daily_fallback=daily_fallback)
        self.fixture.coordinator = self.coordinator
        self.fixture.queue.hass = self.coordinator.hass

    async def test_old_daily_and_recent_hours_share_one_correct_monthly_baseline(self):
        first, detailed = date(2026, 6, 1), date(2026, 6, 3)
        daily = [
            reading("2026-06-01T23:02:00+01:00", 101, 1),
            reading("2026-06-02T23:02:00+01:00", 103, 2),
            reading("2026-06-03T23:02:00+01:00", 103.024, 0.024),
        ]
        hourly = provider_hours(detailed, [1] * 24, counter_before="103.000")
        await self.import_rows(daily, hourly, first, detailed + timedelta(days=1))

        rows = self.fixture.database_rows(ENERGY_ID)
        self.assertEqual(len(rows), 26)
        self.assertEqual(rows[0][1:], (101, 1))
        self.assertAlmostEqual(rows[-1][2], 3.024)
        days = self.recorder_period(first, detailed + timedelta(days=1), "day")
        self.assertEqual([round(row["change"]) for row in days], [1000, 2000, 24])
        month = self.recorder_period(first, date(2026, 7, 1), "month")
        self.assertEqual(len(month), 1)
        self.assertAlmostEqual(month[0]["change"], 3024)
        self.assertEqual(
            self.coordinator._ledger,
            {
                "DAILY": normalize_readings(daily, Frequency.DAILY, MADRID),
                "HOURLY": normalize_readings(hourly, Frequency.HOURLY, MADRID),
            },
        )
        self.assert_native_preserved()

    async def test_complete_hours_replace_one_old_aggregate_without_surplus_or_double_count(self):
        day = date(2026, 6, 10)
        daily = [reading("2026-06-10T23:02:00+01:00", 100.024, 0.024)]
        hourly = provider_hours(day, [1] * 24)
        await self.import_rows(daily, hourly[:4], day, day + timedelta(days=1))
        before_daily = self.fixture.database_rows(DAILY_ID)
        old_energy = self.fixture.database_rows(ENERGY_ID)
        self.assertEqual(len(old_energy), 1)
        self.assertAlmostEqual(old_energy[0][2], 0.024)

        await self.import_rows(daily, hourly, day, day + timedelta(days=1), (Frequency.HOURLY,))
        energy = self.fixture.database_rows(ENERGY_ID)
        self.assertEqual(len(energy), 24)
        self.assertIn(old_energy[0][0], {row[0] for row in energy})
        self.assertAlmostEqual(energy[-1][2], 0.024)
        self.assertEqual(self.fixture.database_rows(DAILY_ID), before_daily)
        hours = self.recorder_period(day, day + timedelta(days=1), "hour")
        self.assertEqual(len(hours), 24)
        self.assertTrue(all(abs(row["change"] - 1) < 1e-6 for row in hours))
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.assert_native_preserved()

    async def test_late_daily_total_replaces_partial_energy_without_changing_reported_hours(self):
        day = date(2026, 6, 10)
        hourly = provider_hours(day, [1] * 4)
        await self.import_rows([], hourly, day, day + timedelta(days=1))
        raw_hours = self.fixture.database_rows(HOURLY_ID)
        self.assertEqual(len(self.fixture.database_rows(ENERGY_ID)), 4)
        daily = [reading("2026-06-10T23:02:00+01:00", 100.024, 0.024)]
        await self.import_rows(daily, [], day, day + timedelta(days=1), (Frequency.DAILY,))
        energy = self.fixture.database_rows(ENERGY_ID)
        self.assertEqual(len(energy), 1)
        self.assertAlmostEqual(energy[0][2], 0.024)
        self.assertEqual(self.fixture.database_rows(HOURLY_ID), raw_hours)
        self.fixture.queue.async_clear_statistics.assert_called_once_with([ENERGY_ID])
        self.assert_native_preserved()

    async def test_old_daily_correction_rebuilds_energy_sums_without_changing_later_consumption(
        self,
    ):
        first, detailed = date(2026, 6, 1), date(2026, 6, 3)
        daily = [
            reading("2026-06-01T23:02:00+01:00", 101, 1),
            reading("2026-06-02T23:02:00+01:00", 103, 2),
            reading("2026-06-03T23:02:00+01:00", 103.024, 0.024),
        ]
        hourly = provider_hours(detailed, [1] * 24, counter_before="103.000")
        await self.import_rows(daily, hourly, first, detailed + timedelta(days=1))
        raw_hours = self.fixture.database_rows(HOURLY_ID)
        later_daily = {
            key: row.copy()
            for key, row in self.coordinator._ledger["DAILY"].items()
            if datetime.fromisoformat(key).astimezone(MADRID).date() != first
        }
        correction = [reading("2026-06-01T23:03:00+01:00", 101.001, 1.001)]
        await self.import_rows(correction, [], first, first + timedelta(days=1), (Frequency.DAILY,))
        self.assertAlmostEqual(self.fixture.database_rows(ENERGY_ID)[-1][2], 3.025)
        self.assertEqual(self.fixture.database_rows(HOURLY_ID), raw_hours)
        self.assertEqual(
            {key: self.coordinator._ledger["DAILY"][key] for key in later_daily}, later_daily
        )
        days = self.recorder_period(first, detailed + timedelta(days=1), "day")
        self.assertEqual([round(row["change"]) for row in days], [1001, 2000, 24])
        hours = self.recorder_period(detailed, detailed + timedelta(days=1), "hour")
        self.assertTrue(all(abs(row["change"] - 1) < 1e-6 for row in hours))
        self.assert_native_preserved()

    async def test_restart_replays_identical_energy_without_changing_raw_series(self):
        day = date(2026, 7, 10)
        daily = [reading("2026-07-10T23:02:00+01:00", 100.3, 0.3)]
        await self.import_rows(daily, [], day, day + timedelta(days=1))
        sources = self.source_rows()
        before = self.fixture.database_rows(ENERGY_ID)
        self.restart()
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertEqual(self.fixture.database_rows(ENERGY_ID), before)
        self.assertEqual(self.source_rows(), sources)
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.assert_native_preserved()

    async def test_winter_handoff_uses_provider_blocks_across_a_calendar_month_boundary(self):
        first, detailed = date(2026, 1, 31), date(2026, 2, 1)
        daily = [
            reading("2026-01-31T23:02:00+01:00", 100.1, 0.1),
            reading("2026-02-01T23:02:00+01:00", 100.124, 0.024),
        ]
        hourly = provider_hours(detailed, [1] * 24, counter_before="100.100")
        # A winter provider block begins at 23:00 on the previous civil date.
        await self.import_rows(daily, hourly, first, date(2026, 2, 2))
        energy = self.fixture.database_rows(ENERGY_ID)
        self.assertEqual(len(energy), 25)
        self.assertAlmostEqual(energy[-1][2], 0.124)
        january = self.recorder_period(date(2026, 1, 1), detailed, "month")
        february = self.recorder_period(detailed, date(2026, 3, 1), "month")
        self.assertEqual(round(january[0]["change"]), 101)
        self.assertEqual(round(february[0]["change"]), 23)
        self.assertEqual(round(january[0]["change"] + february[0]["change"]), 124)
        self.assert_native_preserved()

    async def test_dropped_energy_import_is_detected_and_restart_recovers_from_source(self):
        day = date(2026, 7, 10)
        daily = [reading("2026-07-10T23:02:00+01:00", 100.3, 0.3)]
        real_import = self.fixture.queue.async_import_statistics.side_effect

        def drop_energy(metadata, points, table):
            if metadata["statistic_id"] != ENERGY_ID:
                return real_import(metadata, points, table)

        self.fixture.queue.async_import_statistics.side_effect = drop_energy
        with self.assertRaises(UpdateFailed):
            await self.import_rows(daily, [], day, day + timedelta(days=1))
        self.assertEqual(self.fixture.database_rows(ENERGY_ID), [])
        self.assertTrue(self.coordinator._store.saved["DAILY"])
        sources = self.source_rows()
        self.fixture.queue.async_import_statistics.side_effect = real_import
        self.restart()
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertEqual(len(self.fixture.database_rows(ENERGY_ID)), 1)
        self.assertEqual(self.source_rows(), sources)
        self.assert_native_preserved()

    async def test_truncated_raw_history_blocks_derived_clear_before_any_recorded_loss(self):
        day = date(2026, 7, 10)
        daily = [
            reading("2026-07-10T23:02:00+01:00", 100.3, 0.3),
            reading("2026-07-11T23:02:00+01:00", 100.5, 0.2),
        ]
        await self.import_rows(daily, [], day, day + timedelta(days=2))
        sources = self.source_rows()
        energy = self.fixture.database_rows(ENERGY_ID)
        saved = self.coordinator._store.saved
        saved["DAILY"].pop(min(saved["DAILY"]))
        self.restart()
        await self.coordinator._load_ledger()
        with self.assertRaisesRegex(ValueError, "ledger is missing"):
            await self.coordinator._publish()
        self.assertEqual(self.source_rows(), sources)
        self.assertEqual(self.fixture.database_rows(ENERGY_ID), energy)
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()
        self.assert_native_preserved()

    async def test_lost_projection_metadata_cannot_recompute_an_existing_energy_boundary(self):
        day = date(2026, 7, 10)
        daily = [reading("2026-07-10T23:02:00+01:00", 100.3, 0.3)]
        await self.import_rows(daily, [], day, day + timedelta(days=1))
        sources = self.source_rows()
        energy = self.fixture.database_rows(ENERGY_ID)
        self.coordinator._store.saved.pop("energy_projection_version")
        self.coordinator._store.saved.pop("energy_hourly_since")
        self.restart()
        with self.assertRaises(ValueError):
            await self.coordinator._load_ledger()
        self.assertEqual(self.source_rows(), sources)
        self.assertEqual(self.fixture.database_rows(ENERGY_ID), energy)
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()
        self.assert_native_preserved()

    async def test_missing_source_store_and_raw_statistics_cannot_clear_remaining_energy(self):
        day = date(2026, 7, 10)
        daily = [reading("2026-07-10T23:02:00+01:00", 100.3, 0.3)]
        await self.import_rows(daily, [], day, day + timedelta(days=1))
        energy = self.fixture.database_rows(ENERGY_ID)
        self.clear_statistics([DAILY_ID, HOURLY_ID])
        self.coordinator._store.saved = None
        self.restart()
        with self.assertRaises(ValueError):
            await self.coordinator._load_ledger()
        self.assertEqual(self.fixture.database_rows(ENERGY_ID), energy)
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()
        self.assert_native_preserved()

    async def test_old_format_store_is_upgraded_when_no_energy_series_exists(self):
        day = date(2026, 7, 10)
        daily = [reading("2026-07-10T23:02:00+01:00", 100.3, 0.3)]
        await self.import_rows(daily, [], day, day + timedelta(days=1))
        sources = self.source_rows()
        self.clear_statistics([ENERGY_ID])
        self.coordinator._store.saved.pop("energy_projection_version")
        self.coordinator._store.saved.pop("energy_hourly_since")
        self.restart()
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertEqual(self.source_rows(), sources)
        self.assertEqual(len(self.fixture.database_rows(ENERGY_ID)), 1)
        self.assertAlmostEqual(self.fixture.database_rows(ENERGY_ID)[0][2], 0.3)
        self.assertEqual(self.coordinator._store.saved["energy_projection_version"], 1)
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.assert_native_preserved()

    async def test_empty_versioned_sources_cannot_erase_the_only_remaining_energy_copy(self):
        day = date(2026, 7, 10)
        daily = [reading("2026-07-10T23:02:00+01:00", 100.3, 0.3)]
        await self.import_rows(daily, [], day, day + timedelta(days=1))
        energy = self.fixture.database_rows(ENERGY_ID)
        self.clear_statistics([DAILY_ID, HOURLY_ID])
        self.coordinator._store.saved["DAILY"] = {}
        self.coordinator._store.saved["HOURLY"] = {}
        self.restart()
        with self.assertRaises(ValueError):
            await self.coordinator._load_ledger()
            await self.coordinator._publish()
        self.assertEqual(self.fixture.database_rows(ENERGY_ID), energy)
        self.fixture.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()
        self.assert_native_preserved()

    async def test_older_hourly_island_cannot_move_the_durable_detail_boundary(self):
        day, detailed = date(2026, 7, 1), date(2026, 7, 3)
        daily = [
            reading("2026-07-01T23:02:00+01:00", 100.024, 0.024),
            reading("2026-07-02T23:02:00+01:00", 100.048, 0.024),
            reading("2026-07-03T23:02:00+01:00", 100.072, 0.024),
        ]
        partial = provider_hours(date(2026, 7, 2), [1] * 4, counter_before="100.024")
        recent = provider_hours(detailed, [1] * 24, counter_before="100.048")
        self.configure(self.coordinator, daily_fallback=False)
        await self.import_rows(daily, partial + recent, day, detailed + timedelta(days=1))
        self.assertEqual(self.coordinator._store.saved["energy_hourly_since"], "2026-07-03")
        self.assertAlmostEqual(self.fixture.database_rows(ENERGY_ID)[-1][2], 0.072)
        await self.import_rows(
            daily,
            provider_hours(day, [1] * 24),
            day,
            day + timedelta(days=1),
            (Frequency.HOURLY,),
        )
        self.assertEqual(self.coordinator._store.saved["energy_hourly_since"], "2026-07-03")
        self.assertEqual(len(self.fixture.database_rows(ENERGY_ID)), 49)
        self.assertAlmostEqual(self.fixture.database_rows(ENERGY_ID)[-1][2], 0.072)
        middle = self.recorder_period(date(2026, 7, 2), detailed, "day")
        self.assertEqual(round(middle[0]["change"]), 24)
        self.assert_native_preserved()

    async def test_interrupted_derived_clear_replays_only_energy_and_preserves_raw_history(self):
        day = date(2026, 7, 10)
        daily = [reading("2026-07-10T23:02:00+01:00", 100.024, 0.024)]
        hourly = provider_hours(day, [1] * 24)
        self.configure(self.coordinator, daily_fallback=False)
        await self.import_rows(daily, hourly[:4], day, day + timedelta(days=1))
        self.assertEqual(len(self.fixture.database_rows(ENERGY_ID)), 4)
        sources = self.source_rows()

        self.configure(self.coordinator, daily_fallback=True)
        original_wait = self.fixture.queue.async_block_till_done.side_effect
        self.fixture.queue.async_block_till_done.side_effect = RuntimeError("Recorder interrupted")
        with self.assertRaises(RuntimeError):
            await self.coordinator._publish()
        self.assertEqual(self.fixture.database_rows(ENERGY_ID), [])
        self.fixture.queue.async_clear_statistics.assert_called_once_with([ENERGY_ID])
        self.assertEqual(self.source_rows(), sources)

        self.fixture.queue.async_block_till_done.side_effect = original_wait
        self.restart(daily_fallback=True)
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        energy = self.fixture.database_rows(ENERGY_ID)
        self.assertEqual(len(energy), 1)
        self.assertAlmostEqual(energy[0][2], 0.024)
        self.assertEqual(self.source_rows(), sources)
        self.assert_native_preserved()
