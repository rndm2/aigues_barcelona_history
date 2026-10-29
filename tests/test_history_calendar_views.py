"""Compare actual HA hour/day views of one canonical statistic in SQLite."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from homeassistant.components.recorder import statistics as ha_statistics
from test_aigues_barcelona_history import HOURLY_ID, MADRID, reading
import test_history_integrity as integrity_fixture

from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.statistics import Frequency

UTC = timezone.utc


class CalendarViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse the actual SQLite fixture without inheriting/rerunning its tests.
        self.fixture = integrity_fixture.RecorderSQLiteIntegrityTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.coordinator = self.fixture.coordinator

    @staticmethod
    def bounds(day):
        return (
            datetime.combine(day, time.min, tzinfo=MADRID).astimezone(UTC),
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=MADRID).astimezone(UTC),
        )

    def ha_rows(self, day, period):
        start, stop = self.bounds(day)
        # HA's day query includes the calendar date containing end_time.
        # End just before the following midnight to select precisely one day.
        end = stop - timedelta(microseconds=1) if period == "day" else stop
        rows = ha_statistics.statistics_during_period(
            self.coordinator.hass,
            start,
            end,
            {HOURLY_ID},
            period,
            {"volume": "L"},
            {"state", "sum", "change"},
        ).get(HOURLY_ID, [])
        return [
            row
            for row in rows
            if datetime.fromtimestamp(row["start"], UTC).astimezone(MADRID).date() == day
        ]

    async def import_rows(self, rows, start, stop):
        self.coordinator._api.consumptions.return_value = rows
        await self.coordinator._import_range(start, stop, (Frequency.HOURLY,))

    def assert_native_unchanged(self):
        self.assertEqual(self.fixture.database_rows(self.fixture.old_id), self.fixture.old_snapshot)

    async def test_local_calendar_day_uses_its_actual_boundary_hours(self):
        # Synthetic fixed-CET report: 120 L. The HA day includes a preceding
        # 15 L interval and excludes the outgoing 5 L interval, giving 130 L.
        raw_day_deltas_l = [5] * 24
        self.assertEqual(sum(raw_day_deltas_l), 120)
        counter = Decimal("100.000")
        rows = []
        observations = [("2026-10-22T23:01:44+01:00", 15)] + [
            (f"2026-10-23T{hour:02}:01:00+01:00", delta)
            for hour, delta in enumerate(raw_day_deltas_l)
        ]
        for timestamp, delta_l in observations:
            delta = Decimal(delta_l) / 1000
            counter += delta
            rows.append(reading(timestamp, float(counter), float(delta)))
        await self.import_rows(rows, date(2026, 10, 22), date(2026, 10, 25))

        hours = self.ha_rows(date(2026, 10, 23), "hour")
        days = self.ha_rows(date(2026, 10, 23), "day")
        self.assertEqual(len(hours), 24)
        self.assertEqual(len(days), 1)
        self.assertAlmostEqual(sum(row["change"] for row in hours), 130)
        self.assertAlmostEqual(days[0]["change"], 130)
        self.assertAlmostEqual(hours[0]["change"], 15)
        self.assertEqual(
            datetime.fromtimestamp(hours[0]["start"], UTC), datetime(2026, 10, 22, 22, tzinfo=UTC)
        )
        self.assertAlmostEqual(days[0]["state"], 100130)
        self.assert_native_unchanged()

    async def test_actual_ha_calendar_reduction_handles_23_and_25_hour_days(self):
        for day, expected_hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25)):
            with self.subTest(day=day):
                start, stop = self.bounds(day)
                rows = [
                    reading(
                        (start + timedelta(hours=index, minutes=1)).isoformat(),
                        100 + index / 1000,
                        0.001,
                    )
                    for index in range(expected_hours)
                ]
                self.assertEqual((stop - start).total_seconds(), expected_hours * 3600)
                await self.import_rows(rows, day - timedelta(days=1), day + timedelta(days=1))
                hours = self.ha_rows(day, "hour")
                days = self.ha_rows(day, "day")
                self.assertEqual(len(hours), expected_hours)
                self.assertEqual(len(days), 1)
                self.assertAlmostEqual(sum(row["change"] for row in hours), expected_hours)
                self.assertAlmostEqual(days[0]["change"], expected_hours)
                local_hours = [
                    datetime.fromtimestamp(row["start"], UTC).astimezone(MADRID) for row in hours
                ]
                if expected_hours == 23:
                    self.assertNotIn(2, [value.hour for value in local_hours])
                else:
                    repeated = [value for value in local_hours if value.hour == 2]
                    self.assertEqual(len(repeated), 2)
                    self.assertEqual(
                        {value.utcoffset() for value in repeated},
                        {timedelta(hours=1), timedelta(hours=2)},
                    )
                self.assert_native_unchanged()

    async def test_missing_hour_remains_absent_and_day_view_sums_only_known_hours(self):
        day = date(2026, 10, 26)
        start, _ = self.bounds(day)
        omitted = start + timedelta(hours=5)
        rows = [
            reading(
                (start + timedelta(hours=index, minutes=1)).isoformat(), 100 + index / 1000, 0.001
            )
            for index in range(24)
            if index != 5
        ]
        await self.import_rows(rows, day - timedelta(days=1), day + timedelta(days=1))
        hours = self.ha_rows(day, "hour")
        days = self.ha_rows(day, "day")
        self.assertEqual(len(hours), 23)
        self.assertNotIn(omitted.timestamp(), [row["start"] for row in hours])
        self.assertEqual(len(days), 1)
        self.assertAlmostEqual(days[0]["change"], 23)
        self.assertAlmostEqual(sum(row["change"] for row in hours), 23)
        self.assert_native_unchanged()

    async def test_partial_backfill_and_correction_preserve_outside_consumption(self):
        day = date(2026, 10, 23)
        initial = [
            reading("2026-10-23T00:01:00+02:00", 100.001, 0.001),
            reading("2026-10-23T02:01:00+02:00", 100.003, 0.001),
            reading("2026-10-24T00:01:00+02:00", 100.004, 0.001),
        ]
        await self.import_rows(initial, day, day + timedelta(days=2))
        outside_key = "2026-10-23T22:00:00+00:00"
        outside_source = deepcopy(self.coordinator._ledger["HOURLY"][outside_key])
        self.assertAlmostEqual(self.ha_rows(day, "day")[0]["change"], 2)
        self.assertAlmostEqual(self.ha_rows(day + timedelta(days=1), "day")[0]["change"], 1)

        partial_update = [
            reading("2026-10-23T00:01:00+02:00", 100.001, 0.002),
            reading("2026-10-23T01:01:00+02:00", 100.002, 0.001),
            # A provider response must not overwrite this outside the requested day.
            reading("2026-10-24T00:01:00+02:00", 999, 999),
        ]
        await self.import_rows(partial_update, day, day + timedelta(days=1))
        self.assertEqual(len(self.ha_rows(day, "hour")), 3)
        self.assertAlmostEqual(self.ha_rows(day, "day")[0]["change"], 4)
        self.assertAlmostEqual(sum(row["change"] for row in self.ha_rows(day, "hour")), 4)
        self.assertEqual(self.coordinator._ledger["HOURLY"][outside_key], outside_source)
        self.assertAlmostEqual(self.ha_rows(day + timedelta(days=1), "day")[0]["change"], 1)
        outside_database = [
            row
            for row in self.fixture.database_rows(HOURLY_ID)
            if row[0] == datetime(2026, 10, 23, 22, tzinfo=UTC).timestamp()
        ]
        self.assertAlmostEqual(outside_database[0][2], 0.005)

        before_repeat = self.fixture.database_rows(HOURLY_ID)
        await self.import_rows(partial_update, day, day + timedelta(days=1))
        self.assertEqual(self.fixture.database_rows(HOURLY_ID), before_repeat)
        self.assert_native_unchanged()

    async def test_summer_midnight_keeps_current_provider_day_separate_from_future(self):
        now = datetime(2026, 7, 10, 0, 30, tzinfo=MADRID)
        provider_today = date(2026, 7, 9)
        hourly = [
            reading("2026-07-02T23:01:00+01:00", 100.010, 0.010),
            reading("2026-07-09T23:01:00+01:00", 100.025, 0.015),
        ]
        daily = [reading("2026-07-09T23:01:00+01:00", 100.025, 0.015)]

        def provider_window(start, end, contract, *, frequency):
            # Reproduce the upstream behavior: a request including a future
            # provider date can be empty despite an available midnight reading.
            limit = provider_today + timedelta(days=1) if frequency == "HOURLY" else provider_today
            if end > limit:
                return []
            stop = end if frequency == "HOURLY" else end + timedelta(days=1)
            source = hourly if frequency == "HOURLY" else daily
            return [
                row
                for row in source
                if start <= datetime.fromisoformat(row["datetime"]).date() < stop
            ]

        self.coordinator._api.consumptions.side_effect = provider_window
        with patch.object(sensor.dt_util, "now", return_value=now):
            await self.coordinator._import_range(
                date(2026, 7, 3), date(2026, 7, 11), tuple(Frequency)
            )
            count_before_future = self.coordinator._api.consumptions.call_count
            self.assertEqual(
                await self.coordinator._fetch(
                    date(2026, 7, 10), date(2026, 7, 11), Frequency.DAILY
                ),
                {},
            )
            self.assertEqual(self.coordinator._api.consumptions.call_count, count_before_future)

        calls = self.coordinator._api.consumptions.call_args_list
        for request in calls:
            frequency = request.kwargs["frequency"]
            last_allowed = (
                provider_today + timedelta(days=1) if frequency == "HOURLY" else provider_today
            )
            self.assertLessEqual(request.args[1], last_allowed)
        self.assertTrue(
            any(
                request.args[:2] == (provider_today, provider_today + timedelta(days=1))
                and request.kwargs["frequency"] == "HOURLY"
                for request in calls
            )
        )
        self.assertTrue(
            any(
                request.args[:2] == (provider_today, provider_today)
                and request.kwargs["frequency"] == "DAILY"
                for request in calls
            )
        )
        hours = self.ha_rows(date(2026, 7, 10), "hour")
        self.assertEqual(len(hours), 1)
        self.assertEqual(
            datetime.fromtimestamp(hours[0]["start"], UTC), datetime(2026, 7, 9, 22, tzinfo=UTC)
        )
        self.assertAlmostEqual(hours[0]["change"], 15)
        self.assertAlmostEqual(self.ha_rows(date(2026, 7, 10), "day")[0]["change"], 15)
        self.assert_native_unchanged()
