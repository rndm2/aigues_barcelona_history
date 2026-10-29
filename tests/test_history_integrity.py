"""Independent integrity oracles for interval boundaries and recorder recovery."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.components.recorder import statistics as ha_statistics
from homeassistant.components.recorder import util as recorder_util
from homeassistant.components.recorder.db_schema import (
    SCHEMA_VERSION,
    Statistics,
    StatisticsMeta,
)
from homeassistant.components.recorder.table_managers.statistics_meta import StatisticsMetaManager
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.helpers.update_coordinator import UpdateFailed
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from test_aigues_barcelona_history import (
    DAILY_ID,
    HOURLY_ID,
    MADRID,
    FakeStore,
    OfflineTests,
    make_coordinator,
    reading,
)

from custom_components.aigues_barcelona_history.statistics import (
    Frequency,
    canonical_start,
    normalize_readings,
    validate_saved_ledger,
)

UTC = timezone.utc


class IntervalIntegrityTests(unittest.TestCase):
    def test_hourly_bounds_exclude_the_next_ha_calendar_day(self):
        rows = [
            reading("2026-10-28T23:30:00+01:00", 10, 0.1),
            reading("2026-10-29T00:45:00+01:00", 11, 0.2),
        ]
        ledger = normalize_readings(
            rows,
            Frequency.HOURLY,
            MADRID,
            startdate=date(2026, 10, 28),
            enddate=date(2026, 10, 29),
        )
        self.assertEqual(list(ledger), ["2026-10-28T22:00:00+00:00"])
        self.assertEqual(next(iter(ledger.values()))["delta"], 0.1)
        self.assertEqual(next(iter(ledger.values()))["state"], 10)

    def test_outside_rows_are_still_individually_validated(self):
        rows = [reading("2026-10-29T00:45:00+02:00", 11, float("nan"))]
        with self.assertRaises(ValueError):
            normalize_readings(
                rows,
                Frequency.HOURLY,
                MADRID,
                startdate=date(2026, 10, 28),
                enddate=date(2026, 10, 29),
            )

    def test_bounds_are_half_open_provider_dates_for_both_frequencies(self):
        rows = [
            reading("2026-10-27T23:55:00+01:00", 10, 0.1),
            reading("2026-10-28T00:05:00+01:00", 11, 0.2),
            reading("2026-10-28T23:55:00+01:00", 12, 0.3),
            reading("2026-10-29T00:05:00+01:00", 13, 0.4),
        ]
        for frequency in Frequency:
            with self.subTest(frequency=frequency):
                ledger = normalize_readings(
                    rows,
                    frequency,
                    MADRID,
                    startdate=date(2026, 10, 28),
                    enddate=date(2026, 10, 29),
                )
                self.assertEqual(
                    {row["observed_at"] for row in ledger.values()},
                    {"2026-10-28T23:55:00+01:00"}
                    if frequency is Frequency.DAILY
                    else {
                        "2026-10-28T00:05:00+01:00",
                        "2026-10-28T23:55:00+01:00",
                    },
                )

    def test_invalid_date_bounds_fail_even_when_response_is_empty(self):
        for bounds in (
            {"startdate": datetime(2026, 10, 28), "enddate": date(2026, 10, 29)},
            {"startdate": date(2026, 10, 29), "enddate": date(2026, 10, 28)},
        ):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                normalize_readings([], Frequency.HOURLY, MADRID, **bounds)

    def test_saved_hour_must_match_its_observation_not_just_an_aligned_key(self):
        ledger = normalize_readings(
            [reading("2026-10-28T10:01:00Z", 10, 0.1)], Frequency.HOURLY, MADRID
        )
        record = next(iter(ledger.values()))
        record["start"] = "2026-10-29T10:00:00+00:00"
        shifted = {record["start"]: record}
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_saved_ledger(shifted, Frequency.HOURLY, MADRID)

    def test_saved_daily_series_rejects_hourly_points(self):
        hourly = normalize_readings(
            [reading("2026-10-28T10:01:00Z", 10, 0.1)], Frequency.HOURLY, MADRID
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_saved_ledger(hourly, Frequency.DAILY, MADRID)

    def test_saved_utc_aliases_canonicalize_without_changing_an_interval(self):
        ledger = normalize_readings(
            [reading("2026-10-28T10:01:00Z", 10, 0.1)], Frequency.HOURLY, MADRID
        )
        row = next(iter(ledger.values()))
        aliased = {"2026-10-28T10:00:00Z": {**row, "start": "2026-10-28T10:00:00Z"}}
        self.assertEqual(validate_saved_ledger(aliased, Frequency.HOURLY, MADRID), ledger)

    def test_one_canonical_rule_retains_both_autumn_hours(self):
        self.assertEqual(
            [
                canonical_start(datetime.fromisoformat(timestamp), Frequency.HOURLY, MADRID)
                for timestamp in ("2026-10-25T02:15:00+02:00", "2026-10-25T02:15:00+01:00")
            ],
            [datetime(2026, 10, 25, 0, tzinfo=UTC), datetime(2026, 10, 25, 1, tzinfo=UTC)],
        )


class CoordinatorIntegrityTests(OfflineTests):
    async def test_partial_source_ledger_cannot_rebase_existing_database_history(self):
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T23:00:00+01:00", 101, 1),
            reading("2026-10-28T23:00:00+01:00", 103, 2),
            reading("2026-10-29T23:00:00+01:00", 106, 3),
        ]
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 30), (Frequency.DAILY,)
        )
        previous = deepcopy(self.recorded)
        saved = deepcopy(self.coordinator._store.saved)
        saved["DAILY"].pop(min(saved["DAILY"]))
        self.coordinator = make_coordinator(FakeStore(saved))
        self.queue.async_import_statistics.reset_mock()
        with self.assertRaises((ValueError, UpdateFailed)):
            await self.coordinator._import_range(
                date(2026, 10, 28), date(2026, 10, 30), (Frequency.DAILY,)
            )
        self.assertEqual(self.recorded, previous)
        self.queue.async_import_statistics.assert_not_called()
        self.coordinator._api.consumptions.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_present_but_empty_source_store_cannot_reset_existing_baseline(self):
        await self.import_frequency(Frequency.DAILY, [reading("2026-10-27T23:00:00+01:00", 101, 1)])
        previous = deepcopy(self.recorded)
        self.coordinator = make_coordinator(FakeStore({"DAILY": {}, "HOURLY": {}}))
        self.queue.async_import_statistics.reset_mock()
        with self.assertRaises((ValueError, UpdateFailed)):
            await self.coordinator._import_range(
                date(2026, 10, 28), date(2026, 10, 29), (Frequency.DAILY,)
            )
        self.assertEqual(self.recorded, previous)
        self.queue.async_import_statistics.assert_not_called()
        self.coordinator._api.consumptions.assert_not_called()

    async def test_silently_dropped_recorder_import_is_detected_then_retried(self):
        record_effect = self.queue.async_import_statistics.side_effect
        self.queue.async_import_statistics.side_effect = None
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T23:00:00+01:00", 101, 1)
        ]
        with self.assertRaises(UpdateFailed):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 28), (Frequency.DAILY,)
            )
        self.assertEqual(len(self.coordinator._store.saved["DAILY"]), 1)
        self.queue.async_import_statistics.assert_called_once()

        self.queue.async_import_statistics.side_effect = record_effect
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 28), (Frequency.DAILY,)
        )
        self.assertEqual(self.queue.async_import_statistics.call_count, 2)
        self.assertTrue(self.recorded.get(DAILY_ID))

    async def test_recorder_readback_checks_middle_rows_not_only_latest_sum(self):
        record_effect = self.queue.async_import_statistics.side_effect

        def drop_middle(metadata, points, table):
            record_effect(
                metadata, [point for index, point in enumerate(points) if index != 1], table
            )

        self.queue.async_import_statistics.side_effect = drop_middle
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T10:00:00Z", 100.1, 0.1),
            reading("2026-10-27T11:00:00Z", 100.3, 0.2),
            reading("2026-10-27T12:00:00Z", 100.6, 0.3),
        ]
        with self.assertRaises(UpdateFailed):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
            )
        self.queue.async_import_statistics.side_effect = record_effect
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
        )
        self.assertEqual(self.queue.async_import_statistics.call_count, 2)
        self.assertEqual(len(self.recorded[HOURLY_ID]), 3)

    async def test_sum_overflow_does_not_poison_store_and_next_batch_can_recover(self):
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T23:00:00+01:00", 1e308, 1e308),
            reading("2026-10-28T23:00:00+01:00", 1e308, 1e308),
        ]
        with self.assertRaises(ValueError):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
            )
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T23:00:00+01:00", 100.1, 0.1)
        ]
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
        )
        self.coordinator._store.async_save.assert_awaited_once()
        self.assertEqual(self.publications()[DAILY_ID][1][0]["sum"], 0.1)

    async def test_fetch_filters_padded_responses_to_requested_ha_dates(self):
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-28T23:30:00+01:00", 10, 0.1),
            reading("2026-10-29T00:45:00+01:00", 11, 0.2),
        ]
        rows = await self.coordinator._fetch(
            date(2026, 10, 28), date(2026, 10, 29), Frequency.HOURLY
        )
        self.assertEqual(list(rows), ["2026-10-28T22:00:00+00:00"])
        self.assertEqual(next(iter(rows.values()))["delta"], 0.1)
        self.coordinator._store.async_save.assert_not_awaited()


class RecorderSQLiteIntegrityTests(OfflineTests):
    """Run actual HA import, ORM, metadata and change calculations in memory."""

    def setUp(self):
        super().setUp()
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        StatisticsMeta.__table__.create(self.engine)
        Statistics.__table__.create(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.coordinator.hass.states = SimpleNamespace(get=lambda _: None)
        self.queue.hass = self.coordinator.hass
        self.queue.thread_id = threading.get_ident()
        self.queue.schema_version = SCHEMA_VERSION
        self.queue.get_session = self.sessions
        self.queue.database_engine = SimpleNamespace(
            optimizer=SimpleNamespace(slow_dependent_subquery=False)
        )
        self.queue.statistics_meta_manager = StatisticsMetaManager(self.queue)
        self.enterContext(patch.object(recorder_util, "get_instance", return_value=self.queue))
        self.enterContext(patch.object(recorder_helper, "get_instance", return_value=self.queue))
        self.last_statistics.side_effect = ha_statistics.get_last_statistics
        self.metadata.side_effect = ha_statistics.get_metadata
        self.queue.async_import_statistics.side_effect = lambda metadata, points, table: (
            ha_statistics.import_statistics(self.queue, metadata, points, table)
        )

        # A native statistic sharing the meter's history must remain independent.
        self.old_id = "sensor.contador_test123"
        old_metadata = {
            "mean_type": 0,
            "unit_class": "volume",
            "has_sum": True,
            "name": "Original meter",
            "source": "recorder",
            "statistic_id": self.old_id,
            "unit_of_measurement": "m³",
        }
        ha_statistics.import_statistics(
            self.queue,
            old_metadata,
            [{"start": datetime(2026, 10, 27, 9, tzinfo=UTC), "state": 100, "sum": 100}],
            Statistics,
        )
        self.old_snapshot = self.database_rows(self.old_id)

    def database_rows(self, statistic_id):
        with self.sessions() as session:
            rows = session.execute(
                select(Statistics.start_ts, Statistics.state, Statistics.sum)
                .join(StatisticsMeta, Statistics.metadata_id == StatisticsMeta.id)
                .where(StatisticsMeta.statistic_id == statistic_id)
                .order_by(Statistics.start_ts)
            )
            return [tuple(row) for row in rows]

    def actual_ha_changes(self, statistic_id, start=None):
        result = ha_statistics.get_last_statistics(
            self.coordinator.hass, 100, statistic_id, False, {"state", "sum"}
        )
        result[statistic_id].sort(key=lambda row: row["start"])
        if start is None:
            start = datetime.fromtimestamp(result[statistic_id][0]["start"], tz=UTC)
        else:
            result[statistic_id] = [
                row for row in result[statistic_id] if row["start"] >= start.timestamp()
            ]
        with self.sessions() as session:
            metadata = self.queue.statistics_meta_manager.get_many(
                session, statistic_ids={statistic_id}
            )
            ha_statistics._augment_result_with_change(
                self.coordinator.hass,
                session,
                start,
                None,
                {"change", "state", "sum"},
                Statistics,
                metadata,
                result,
            )
        return result[statistic_id]

    async def test_real_sqlite_sparse_hourly_import_does_not_bridge_meter_jump(self):
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T10:01:00+01:00", 100, 0.2),
            reading("2026-10-28T16:01:00+01:00", 102, 0.3),
        ]
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), (Frequency.HOURLY,)
        )
        rows = self.database_rows(HOURLY_ID)
        self.assertEqual(
            rows,
            [
                (datetime(2026, 10, 27, 9, tzinfo=UTC).timestamp(), 100, 0.2),
                (datetime(2026, 10, 28, 15, tzinfo=UTC).timestamp(), 102, 0.5),
            ],
        )
        self.assertEqual([row["change"] for row in self.actual_ha_changes(HOURLY_ID)], [0.2, 0.3])
        after_gap = self.actual_ha_changes(HOURLY_ID, start=datetime(2026, 10, 28, 15, tzinfo=UTC))
        self.assertEqual([row["change"] for row in after_gap], [0.3])
        self.assertEqual(self.database_rows(self.old_id), self.old_snapshot)

    async def test_real_sqlite_correction_updates_later_sums_and_repeat_is_idempotent(self):
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T23:00:00+01:00", 163.28, 0.851),
            reading("2026-10-28T23:00:00+01:00", 164.036, 0.756),
        ]
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
        )
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T23:00:00+01:00", 163.378, 0.9)
        ]
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
        )
        self.assertEqual([row[2] for row in self.database_rows(DAILY_ID)], [0.9, 1.656])
        changes = self.actual_ha_changes(DAILY_ID)
        self.assertAlmostEqual(changes[0]["change"], 0.9)
        self.assertAlmostEqual(changes[1]["change"], 0.756)
        before_repeat = self.database_rows(DAILY_ID)
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), (Frequency.DAILY,)
        )
        self.assertEqual(self.database_rows(DAILY_ID), before_repeat)
        self.assertEqual(self.database_rows(self.old_id), self.old_snapshot)

    async def test_real_swallowed_sql_error_is_detected_by_readback_then_recovers(self):
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T10:01:00Z", 100.1, 0.1)
        ]
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
        )
        before_failure = self.database_rows(HOURLY_ID)
        self.coordinator._api.consumptions.return_value = [
            reading("2026-10-27T10:01:00Z", 100.2, 0.2)
        ]
        # HA catches this SQLAlchemyError inside its updater and returns success.
        # History must detect the unchanged database instead of trusting the queue.
        with patch("sqlalchemy.orm.query.Query.update", side_effect=SQLAlchemyError("Injected")):
            with self.assertLogs("homeassistant.components.recorder.statistics", level="ERROR"):
                with self.assertRaises(UpdateFailed):
                    await self.coordinator._import_range(
                        date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
                    )
        self.assertEqual(self.database_rows(HOURLY_ID), before_failure)
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
        )
        self.assertEqual([row[2] for row in self.database_rows(HOURLY_ID)], [0.2])
        self.assertAlmostEqual(self.actual_ha_changes(HOURLY_ID)[0]["change"], 0.2)
        self.assertEqual(self.database_rows(self.old_id), self.old_snapshot)
