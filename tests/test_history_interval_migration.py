"""Versioned interval-end migration and recoverable recorder replacement."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from homeassistant.components.recorder import statistics as ha_statistics
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_aigues_barcelona_history import (
    DAILY_ID,
    HOURLY_ID,
    MADRID,
    FakeStore,
    OfflineTests,
    make_coordinator,
    reading,
)
import test_history_integrity as recorder_fixture

from custom_components.aigues_barcelona_history.statistics import (
    Frequency,
    build_statistics,
    normalize_readings,
)

UTC = timezone.utc
ROWS = [
    reading("2026-10-10T23:02:25+01:00", 100.134, 0.134),
    reading("2026-10-25T02:01:38+01:00", 100.134, 0),
    reading("2026-10-26T00:01:35+01:00", 100.154, 0.020),
]


def legacy_source(rows=ROWS):
    """Construct the documented old format independently of the new rule."""
    hourly = {}
    for row in rows:
        observed = datetime.fromisoformat(row["datetime"])
        start = observed.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        hourly[start.isoformat()] = {
            "start": start.isoformat(),
            "observed_at": row["datetime"],
            "state": row["accumulatedConsumption"],
            "delta": row["deltaConsumption"],
        }
    return {
        "DAILY": normalize_readings(
            [reading("2026-10-10T23:02:25+01:00", 100.134, 0.876)],
            Frequency.DAILY,
            MADRID,
        ),
        "HOURLY": hourly,
    }


class IntervalMigrationTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.queue.async_clear_statistics = Mock(side_effect=self.clear_statistics)

    def clear_statistics(self, ids):
        for statistic_id in ids:
            self.recorded.pop(statistic_id, None)
            self.recorded_metadata.pop(statistic_id, None)

    def seed_legacy(self, rows=ROWS):
        saved = legacy_source(rows)
        for frequency in Frequency:
            self.record_points(
                self.coordinator._statistic_metadata(frequency),
                build_statistics(saved[frequency.value]),
                Statistics,
            )
        self.recorded["sensor.contador_test123"] = {0: {"state": 100, "sum": 100}}
        self.coordinator = make_coordinator(FakeStore(saved))
        return saved

    async def test_migration_saves_pending_before_clear_and_preserves_other_series(self):
        legacy = self.seed_legacy()
        protected = {
            key: deepcopy(self.recorded[key]) for key in (DAILY_ID, "sensor.contador_test123")
        }
        events = []
        store = self.coordinator._store

        async def save(value):
            events.append(("save", value["hourly_replay_pending"]))
            await store.save(value)

        def clear(ids):
            events.append(("clear", ids))
            self.assertTrue(store.saved["hourly_replay_pending"])
            self.clear_statistics(ids)

        store.async_save.side_effect = save
        self.queue.async_clear_statistics.side_effect = clear
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertEqual(events, [("save", True), ("clear", [HOURLY_ID]), ("save", False)])
        self.assertEqual(store.saved["hourly_interval_version"], 2)
        self.assertNotIn("hourly_replay_source_sha256", store.saved)
        self.assertEqual(set(self.coordinator._ledger), {"DAILY", "HOURLY"})
        self.assertEqual(self.coordinator._ledger["DAILY"], legacy["DAILY"])
        for key, old in legacy["HOURLY"].items():
            shifted = (datetime.fromisoformat(key) - timedelta(hours=1)).isoformat()
            self.assertEqual(self.coordinator._ledger["HOURLY"][shifted], {**old, "start": shifted})
        self.assertEqual({key: self.recorded[key] for key in protected}, protected)

    async def test_completed_migration_never_shifts_or_clears_again(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        saved = deepcopy(self.coordinator._store.saved)
        recorded = deepcopy(self.recorded)
        self.queue.async_clear_statistics.reset_mock()
        self.coordinator = make_coordinator(FakeStore(saved))
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertEqual(self.coordinator._store.saved, saved)
        self.assertEqual(self.recorded, recorded)
        self.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_restart_after_pending_save_replays_once(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        saved = deepcopy(self.coordinator._store.saved)
        self.queue.async_clear_statistics.assert_not_called()
        self.coordinator = make_coordinator(FakeStore(saved))
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.queue.async_clear_statistics.assert_called_once_with([HOURLY_ID])
        self.assertFalse(self.coordinator._store.saved["hourly_replay_pending"])

    async def test_dropped_import_keeps_pending_and_restart_recovers(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        self.queue.async_import_statistics.side_effect = lambda *_: None
        with self.assertRaises(UpdateFailed):
            await self.coordinator._publish()
        self.assertTrue(self.coordinator._store.saved["hourly_replay_pending"])
        self.assertNotIn(HOURLY_ID, self.recorded)
        self.queue.async_import_statistics.side_effect = self.record_points
        self.coordinator = make_coordinator(FakeStore(self.coordinator._store.saved))
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertFalse(self.coordinator._store.saved["hourly_replay_pending"])
        self.assertEqual(len(self.recorded[HOURLY_ID]), len(ROWS))

    async def test_failed_clear_keeps_source_and_pending_state_for_retry(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        before = deepcopy(self.recorded)
        self.queue.async_clear_statistics.side_effect = RuntimeError("Recorder unavailable")
        with self.assertRaises(RuntimeError):
            await self.coordinator._publish()
        self.assertEqual(self.recorded, before)
        self.assertTrue(self.coordinator._store.saved["hourly_replay_pending"])
        self.queue.async_clear_statistics.side_effect = self.clear_statistics
        self.coordinator = make_coordinator(FakeStore(self.coordinator._store.saved))
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertFalse(self.coordinator._store.saved["hourly_replay_pending"])

    async def test_failure_after_clear_before_import_is_recovered_on_restart(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        self.queue.async_block_till_done.side_effect = RuntimeError("Recorder stopped")
        with self.assertRaises(RuntimeError):
            await self.coordinator._publish()
        self.assertNotIn(HOURLY_ID, self.recorded)
        self.assertTrue(self.coordinator._store.saved["hourly_replay_pending"])
        self.queue.async_block_till_done.side_effect = None
        self.coordinator = make_coordinator(FakeStore(self.coordinator._store.saved))
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertEqual(len(self.recorded[HOURLY_ID]), len(ROWS))
        self.assertFalse(self.coordinator._store.saved["hourly_replay_pending"])

    async def test_failed_pending_completion_is_recoverable_without_double_shift(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        store = self.coordinator._store
        store.async_save.side_effect = OSError("Storage unavailable")
        with self.assertRaises(OSError):
            await self.coordinator._publish()
        recorded = deepcopy(self.recorded)
        self.assertTrue(store.saved["hourly_replay_pending"])
        self.coordinator = make_coordinator(FakeStore(store.saved))
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertEqual(self.recorded, recorded)
        self.assertFalse(self.coordinator._store.saved["hourly_replay_pending"])

    async def test_failed_pending_save_cannot_clear_or_modify_recorder(self):
        self.seed_legacy()
        before = deepcopy(self.recorded)
        self.coordinator._store.async_save.side_effect = OSError("Storage unavailable")
        with self.assertRaises(OSError):
            await self.coordinator._load_ledger()
        self.assertIsNone(self.coordinator._ledger)
        self.queue.async_clear_statistics.assert_not_called()
        self.assertEqual(self.recorded, before)

    async def test_incomplete_legacy_source_cannot_clear_recorded_history(self):
        self.seed_legacy()
        saved = deepcopy(self.coordinator._store.saved)
        saved["HOURLY"].pop(min(saved["HOURLY"]))
        self.coordinator = make_coordinator(FakeStore(saved))
        with self.assertRaisesRegex(ValueError, "ledger is missing"):
            await self.coordinator._load_ledger()
        self.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_pending_contiguous_head_loss_cannot_clear_recorded_history(self):
        self.seed_legacy(
            [
                reading("2026-10-26T10:01:00+01:00", 100.1, 0.1),
                reading("2026-10-26T11:01:00+01:00", 100.3, 0.2),
                reading("2026-10-26T12:01:00+01:00", 100.6, 0.3),
            ]
        )
        await self.coordinator._load_ledger()
        saved = deepcopy(self.coordinator._store.saved)
        saved["HOURLY"].pop(min(saved["HOURLY"]))
        recorded = deepcopy(self.recorded)
        self.coordinator = make_coordinator(FakeStore(saved))
        with self.assertRaisesRegex(ValueError, "checksum"):
            await self.coordinator._load_ledger()
        self.assertEqual(self.recorded, recorded)
        self.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_pending_value_tamper_cannot_clear_recorded_history(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        saved = deepcopy(self.coordinator._store.saved)
        saved["HOURLY"][min(saved["HOURLY"])]["delta"] += 0.001
        recorded = deepcopy(self.recorded)
        self.coordinator = make_coordinator(FakeStore(saved))
        with self.assertRaisesRegex(ValueError, "checksum"):
            await self.coordinator._load_ledger()
        self.assertEqual(self.recorded, recorded)
        self.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_pending_replay_without_checksum_fails_closed(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        saved = deepcopy(self.coordinator._store.saved)
        saved.pop("hourly_replay_source_sha256")
        self.coordinator = make_coordinator(FakeStore(saved))
        with self.assertRaisesRegex(ValueError, "checksum"):
            await self.coordinator._load_ledger()
        self.queue.async_clear_statistics.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_pending_source_key_order_does_not_change_its_checksum(self):
        self.seed_legacy()
        await self.coordinator._load_ledger()
        saved = deepcopy(self.coordinator._store.saved)
        saved["HOURLY"] = {
            key: dict(reversed(tuple(row.items())))
            for key, row in reversed(tuple(saved["HOURLY"].items()))
        }
        self.coordinator = make_coordinator(FakeStore(saved))
        await self.coordinator._load_ledger()
        await self.coordinator._publish()
        self.assertFalse(self.coordinator._store.saved["hourly_replay_pending"])

    async def test_missing_or_empty_store_does_not_trigger_destructive_replay(self):
        for saved in (None, {"DAILY": {}, "HOURLY": {}}):
            with self.subTest(saved=saved):
                self.coordinator = make_coordinator(FakeStore(saved))
                self.recorded[HOURLY_ID] = {0: {"start": 0, "state": 100, "sum": 100}}
                await self.coordinator._load_ledger()
                with self.assertRaisesRegex(ValueError, "ledger is missing"):
                    await self.coordinator._publish()
                self.queue.async_clear_statistics.assert_not_called()
                self.coordinator._store.async_save.assert_not_awaited()

    async def test_unknown_or_invalid_format_fails_without_persistent_changes(self):
        for version, pending in ((3, False), (2.0, False), (True, False), (2, "yes"), (1, True)):
            with self.subTest(version=version, pending=pending):
                saved = {
                    **legacy_source(),
                    "hourly_interval_version": version,
                    "hourly_replay_pending": pending,
                }
                self.coordinator = make_coordinator(FakeStore(saved))
                with self.assertRaises(ValueError):
                    await self.coordinator._load_ledger()
                self.queue.async_clear_statistics.assert_not_called()
                self.coordinator._store.async_save.assert_not_awaited()


class IntervalEndFetchWindowTests(OfflineTests):
    def test_summer_winter_and_transition_days_fetch_all_closing_readings(self):
        for start, end, expected in (
            (date(2026, 10, 10), date(2026, 10, 11), (date(2026, 10, 10), date(2026, 10, 11))),
            (date(2026, 10, 26), date(2026, 10, 27), (date(2026, 10, 26), date(2026, 10, 28))),
            (date(2026, 3, 29), date(2026, 3, 30), (date(2026, 3, 29), date(2026, 3, 30))),
            (date(2026, 10, 25), date(2026, 10, 26), (date(2026, 10, 25), date(2026, 10, 27))),
        ):
            with self.subTest(start=start):
                self.assertEqual(
                    self.coordinator._fetch_windows(start, end, Frequency.HOURLY), [expected]
                )

    def test_daily_reference_dates_keep_their_inclusive_api_end(self):
        self.assertEqual(
            self.coordinator._fetch_windows(
                date(2026, 10, 26), date(2026, 10, 27), Frequency.DAILY
            ),
            [(date(2026, 10, 26), date(2026, 10, 26))],
        )


class SQLiteIntervalMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_recorder_replaces_only_hourly_series_and_removes_old_tail(self):
        fixture = recorder_fixture.RecorderSQLiteIntegrityTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        # HA enables SQLite foreign keys, including the statistics metadata
        # cascade. The minimal shared fixture otherwise leaves SQLite's default.
        with fixture.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        saved = legacy_source()
        for frequency in Frequency:
            ha_statistics.import_statistics(
                fixture.queue,
                fixture.coordinator._statistic_metadata(frequency),
                build_statistics(saved[frequency.value]),
                Statistics,
            )
        daily_before = fixture.database_rows(DAILY_ID)
        hourly_before = fixture.database_rows(HOURLY_ID)
        fixture.coordinator = make_coordinator(FakeStore(saved))

        def clear(ids):
            ha_statistics.clear_statistics(fixture.queue, ids)

        fixture.queue.async_clear_statistics = Mock(side_effect=clear)
        await fixture.coordinator._load_ledger()
        await fixture.coordinator._publish()
        expected = [
            (timestamp - 3600, state, cumulative) for timestamp, state, cumulative in hourly_before
        ]
        self.assertEqual(fixture.database_rows(HOURLY_ID), expected)
        self.assertEqual(fixture.database_rows(DAILY_ID), daily_before)
        self.assertEqual(fixture.database_rows(fixture.old_id), fixture.old_snapshot)
        fixture.queue.async_clear_statistics.assert_called_once_with([HOURLY_ID])
        self.assertFalse(fixture.coordinator._store.saved["hourly_replay_pending"])
