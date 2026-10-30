"""Independent meter-reading endpoints and actual HA calendar-day regressions."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import unittest

from test_aigues_barcelona_history import DAILY_ID, HOURLY_ID, MADRID, reading
import test_history_calendar_views as calendar_fixture

from custom_components.aigues_barcelona_history.statistics import Frequency

UTC = timezone.utc
CET = timezone(timedelta(hours=1))


def reported_endpoints(day, deltas_l, *, previous_l=0, next_l=0):
    """Model physical counter increments at interval ends without using importer helpers."""
    start = datetime.combine(day, time.min, tzinfo=MADRID).astimezone(UTC)
    increments = [previous_l, *deltas_l, next_l]
    counter = Decimal("100.000")
    rows = []
    for index, delta_l in enumerate(increments):
        delta = Decimal(delta_l) / 1000
        counter += delta
        observed = start + timedelta(hours=index, minutes=2)
        rows.append(reading(observed.astimezone(CET).isoformat(), float(counter), float(delta)))
    return rows


class EndReadingCalendarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse actual HA SQLite fixtures without inheriting and rerunning their tests.
        self.views = calendar_fixture.CalendarViewTests()
        self.addCleanup(self.views.doCleanups)
        self.views.setUp()

    async def test_october_10_real_report_deltas_belong_to_the_hours_ending_at_each_reading(self):
        day = date(2026, 10, 10)
        # Real reported deltas, with the physical counter anonymized by translation.
        deltas_l = [
            12,
            16,
            9,
            75,
            0,
            0,
            2,
            0,
            0,
            0,
            3,
            34,
            20,
            46,
            85,
            62,
            53,
            0,
            0,
            1,
            26,
            160,
            138,
            134,
        ]
        self.assertEqual(sum(deltas_l), 876)
        rows = reported_endpoints(day, deltas_l, previous_l=62, next_l=17)
        for previous, current in zip(rows, rows[1:]):
            self.assertAlmostEqual(
                current["accumulatedConsumption"] - previous["accumulatedConsumption"],
                current["deltaConsumption"],
            )

        await self.views.import_rows(rows, day, day + timedelta(days=1))
        hours = self.views.ha_rows(day, "hour")
        daily = self.views.ha_rows(day, "day")
        self.assertEqual(len(hours), 24)
        self.assertEqual([round(point["change"]) for point in hours], deltas_l)
        self.assertEqual(round(daily[0]["change"]), 876)
        first, last = [
            datetime.fromtimestamp(point["start"], UTC).astimezone(MADRID)
            for point in (hours[0], hours[-1])
        ]
        self.assertEqual((first.day, first.hour, last.day, last.hour), (10, 0, 10, 23))
        final_source = self.views.coordinator._ledger["HOURLY"][
            datetime.fromtimestamp(hours[-1]["start"], UTC).isoformat()
        ]
        end = datetime.fromisoformat(final_source["observed_at"]).astimezone(MADRID)
        self.assertEqual((end.day, end.hour), (11, 0), "Midnight reading closes the previous hour")
        self.views.assert_native_unchanged()

    async def test_winter_day_needs_next_provider_dates_midnight_reading(self):
        day = date(2026, 1, 10)
        deltas_l = list(range(1, 25))
        rows = reported_endpoints(day, deltas_l, previous_l=90, next_l=70)

        def provider_window(start, stop, contract, *, frequency):
            return [
                row
                for row in rows
                if start <= datetime.fromisoformat(row["datetime"]).date() < stop
            ]

        self.views.coordinator._api.consumptions.side_effect = provider_window
        await self.views.coordinator._import_range(
            day, day + timedelta(days=1), (Frequency.HOURLY,)
        )
        hours = self.views.ha_rows(day, "hour")
        self.assertEqual(len(hours), 24)
        self.assertEqual([round(point["change"]) for point in hours], deltas_l)
        self.assertEqual(round(self.views.ha_rows(day, "day")[0]["change"]), 300)
        request = self.views.coordinator._api.consumptions.call_args
        self.assertEqual(request.args[:2], (day, day + timedelta(days=2)))
        self.assertEqual(round(hours[-1]["change"]), 24)
        self.views.assert_native_unchanged()

    async def test_winter_reference_retains_provider_date_total_without_redistributing_hours(self):
        day = date(2026, 1, 10)
        rows = reported_endpoints(day, list(range(1, 25)), previous_l=90, next_l=70)
        provider_day = [
            row for row in rows if datetime.fromisoformat(row["datetime"]).date() == day
        ]
        reference_l = 90 + sum(range(1, 24))
        daily = [
            reading(
                provider_day[-1]["datetime"],
                provider_day[-1]["accumulatedConsumption"],
                reference_l / 1000,
            )
        ]
        self.views.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else rows
        )
        await self.views.coordinator._import_range(day, day + timedelta(days=1), tuple(Frequency))
        self.assertEqual(round(self.views.ha_rows(day, "day")[0]["change"]), 300)
        self.assertEqual(
            round(self.views.fixture.database_rows(DAILY_ID)[0][2] * 1000), reference_l
        )
        self.assertEqual(len(self.views.fixture.database_rows(HOURLY_ID)), 24)
        self.views.assert_native_unchanged()

    async def test_dst_days_use_23_and_25_real_consumption_intervals_with_end_readings(self):
        for day, count in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25)):
            with self.subTest(day=day):
                deltas_l = list(range(1, count + 1))
                rows = reported_endpoints(day, deltas_l, previous_l=90, next_l=70)
                await self.views.import_rows(rows, day, day + timedelta(days=1))
                hours = self.views.ha_rows(day, "hour")
                self.assertEqual(len(hours), count)
                self.assertEqual([round(point["change"]) for point in hours], deltas_l)
                self.assertEqual(round(self.views.ha_rows(day, "day")[0]["change"]), sum(deltas_l))
                local = [
                    datetime.fromtimestamp(point["start"], UTC).astimezone(MADRID)
                    for point in hours
                ]
                repeated = [point for point in local if point.hour == 2]
                self.assertEqual(len(repeated), 0 if count == 23 else 2)
                if count == 25:
                    self.assertEqual(
                        {point.utcoffset() for point in repeated},
                        {timedelta(hours=1), timedelta(hours=2)},
                    )
                self.views.assert_native_unchanged()

    async def test_missing_end_reading_does_not_fabricate_consumption_from_counter_jump(self):
        day = date(2026, 10, 10)
        rows = reported_endpoints(day, [1] * 24, previous_l=90, next_l=70)
        del rows[7]  # The 06:00 consumption interval has no end reading.
        await self.views.import_rows(rows, day, day + timedelta(days=1))
        hours = self.views.ha_rows(day, "hour")
        self.assertEqual(len(hours), 23)
        self.assertTrue(all(round(point["change"]) == 1 for point in hours))
        self.assertEqual(round(self.views.ha_rows(day, "day")[0]["change"]), 23)
        local_hours = [
            datetime.fromtimestamp(point["start"], UTC).astimezone(MADRID).hour for point in hours
        ]
        self.assertNotIn(6, local_hours)
        self.views.assert_native_unchanged()
