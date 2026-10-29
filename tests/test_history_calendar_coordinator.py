"""Calendar ownership, scoped corrections and publication role regressions."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from unittest.mock import call

from homeassistant.helpers.update_coordinator import UpdateFailed
from test_aigues_barcelona_history import (
    CONTRACT,
    DAILY_ID,
    HOURLY_ID,
    OfflineTests,
    reading,
)

from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.statistics import Frequency


class CanonicalCalendarCoordinatorTests(OfflineTests):
    async def test_public_day_and_hour_use_one_series_with_daily_provider_reference(self):
        daily = [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        hourly = [reading("2026-10-27T10:01:28+01:00", 655.0, 0.062)]
        self.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else hourly
        )
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 29), tuple(Frequency)
        )
        expected = {
            "daily": HOURLY_ID,
            "hourly": HOURLY_ID,
            "provider_daily": DAILY_ID,
        }
        self.assertEqual(self.coordinator._data["statistics"], expected)
        meter = sensor.AiguesHistoryMeter(self.coordinator)
        self.assertEqual(meter.extra_state_attributes["statistics"], expected)
        self.assertEqual(self.coordinator._data["hourly_coverage"]["known_intervals"], 1)
        metadata = self.recorded_metadata
        self.assertIn("reported hours", metadata[HOURLY_ID][1]["name"])
        self.assertIn("Provider daily totals (reference)", metadata[DAILY_ID][1]["name"])

    async def test_provider_daily_import_cannot_create_or_change_canonical_hours(self):
        hours = [reading("2026-10-27T10:01:28+01:00", 655.0, 0.062)]
        await self.import_frequency(Frequency.HOURLY, hours)
        previous_rows = deepcopy(self.recorded[HOURLY_ID])
        previous_source = deepcopy(self.coordinator._store.saved["HOURLY"])
        self.queue.async_import_statistics.reset_mock()

        await self.import_frequency(
            Frequency.DAILY,
            [
                reading("2026-10-27T23:01:28+01:00", 655.724, 1.478),
            ],
        )
        self.assertEqual(self.recorded[HOURLY_ID], previous_rows)
        self.assertEqual(self.coordinator._store.saved["HOURLY"], previous_source)
        self.assertEqual(set(self.publications()), {DAILY_ID})
        self.assertEqual(self.coordinator._data["statistics"]["daily"], HOURLY_ID)
        self.assertEqual(self.coordinator._data["imported_intervals"], {"daily": 1, "hourly": 1})

    async def test_already_correct_rows_receive_new_role_name_once_without_value_changes(self):
        await self.import_frequency(
            Frequency.HOURLY,
            [
                reading("2026-10-27T10:01:28+01:00", 655.0, 0.062),
            ],
        )
        previous_rows = deepcopy(self.recorded)
        self.recorded_metadata[HOURLY_ID][1]["name"] = "Legacy hourly display name"
        self.queue.async_import_statistics.reset_mock()
        await self.coordinator._publish()
        self.queue.async_import_statistics.assert_called_once()
        self.assertEqual(self.recorded, previous_rows)
        self.assertEqual(
            self.publications()[HOURLY_ID][0]["name"],
            f"{sensor.NAME} test123: Water consumption (reported hours)",
        )
        self.queue.async_import_statistics.reset_mock()
        await self.coordinator._publish()
        self.queue.async_import_statistics.assert_not_called()
        self.assertEqual(self.recorded, previous_rows)

    async def test_swallowed_role_name_update_is_detected_then_recovered(self):
        await self.import_frequency(
            Frequency.HOURLY,
            [
                reading("2026-10-27T10:01:28+01:00", 655.0, 0.062),
            ],
        )
        previous_rows = deepcopy(self.recorded)
        self.recorded_metadata[HOURLY_ID][1]["name"] = "Legacy hourly display name"
        self.queue.async_import_statistics.reset_mock()
        self.queue.async_import_statistics.side_effect = lambda *args: None
        with self.assertRaisesRegex(UpdateFailed, "series name"):
            await self.coordinator._publish()
        self.assertEqual(self.recorded, previous_rows)
        self.assertEqual(self.recorded_metadata[HOURLY_ID][1]["name"], "Legacy hourly display name")

        self.queue.async_import_statistics.side_effect = self.record_points
        await self.coordinator._publish()
        self.assertIn("reported hours", self.recorded_metadata[HOURLY_ID][1]["name"])
        self.assertEqual(self.recorded, previous_rows)
        self.queue.async_import_statistics.reset_mock()
        await self.coordinator._publish()
        self.queue.async_import_statistics.assert_not_called()

    async def test_partial_range_updates_real_hours_and_preserves_outside_consumption(self):
        original = [
            reading("2026-10-26T12:10:00+01:00", 100.1, 0.1),
            reading("2026-10-27T10:50:00+01:00", 100.3, 0.2),
            reading("2026-10-27T12:20:00+01:00", 100.6, 0.3),
            reading("2026-10-28T12:20:00+01:00", 101.0, 0.4),
        ]
        self.coordinator._api.consumptions.return_value = original
        await self.coordinator._import_range(
            date(2026, 10, 26), date(2026, 10, 29), (Frequency.HOURLY,)
        )
        source_before = deepcopy(self.coordinator._store.saved["HOURLY"])
        rows_before = deepcopy(self.recorded[HOURLY_ID])
        corrected = [
            # The latest response can correct the minute backwards in this hour.
            reading("2026-10-27T10:05:00+01:00", 100.35, 0.25),
            reading("2026-10-27T11:10:00+01:00", 100.4, 0.05),
            # Extra API rows outside the requested HA dates must be ignored.
            reading("2026-10-26T12:59:00+01:00", 999, 99),
            reading("2026-10-28T12:59:00+01:00", 999, 99),
        ]
        self.coordinator._api.consumptions.return_value = corrected
        self.queue.async_import_statistics.reset_mock()
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
        )
        source = self.coordinator._store.saved["HOURLY"]
        self.assertEqual(len(source), 5, "Only one newly reported hour is added")
        for key in (
            "2026-10-26T11:00:00+00:00",
            "2026-10-27T11:00:00+00:00",
            "2026-10-28T11:00:00+00:00",
        ):
            self.assertEqual(source[key], source_before[key])
        self.assertEqual(
            source["2026-10-27T09:00:00+00:00"]["observed_at"], "2026-10-27T10:05:00+01:00"
        )
        self.assertEqual(source["2026-10-27T09:00:00+00:00"]["delta"], 0.25)
        rows = [self.recorded[HOURLY_ID][key] for key in sorted(self.recorded[HOURLY_ID])]
        self.assertEqual([row["sum"] for row in rows], [0.1, 0.35, 0.4, 0.7, 1.1])
        previous = 0
        changes = []
        for row in rows:
            changes.append(row["sum"] - previous)
            previous = row["sum"]
        for actual, expected in zip(changes, (0.1, 0.25, 0.05, 0.3, 0.4)):
            self.assertAlmostEqual(actual, expected)
        outside_after = datetime.fromisoformat("2026-10-28T11:00:00+00:00").timestamp()
        self.assertEqual(rows_before[outside_after]["sum"], 1.0)
        self.assertEqual(self.recorded[HOURLY_ID][outside_after]["sum"], 1.1)
        self.assertEqual(self.recorded[HOURLY_ID][outside_after]["state"], 101.0)

        self.queue.async_import_statistics.reset_mock()
        before_repeat = deepcopy(self.recorded)
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
        )
        self.assertEqual(self.recorded, before_repeat)
        self.queue.async_import_statistics.assert_not_called()

    async def test_empty_partial_response_keeps_all_previous_reported_hours(self):
        await self.import_frequency(
            Frequency.HOURLY,
            [
                reading("2026-10-27T10:01:00+01:00", 100.1, 0.1),
                reading("2026-10-28T10:01:00+01:00", 100.3, 0.2),
            ],
        )
        before = deepcopy(self.coordinator._store.saved)
        self.queue.async_import_statistics.reset_mock()
        self.coordinator._api.consumptions.return_value = []
        await self.coordinator._import_range(
            date(2026, 10, 27), date(2026, 10, 28), (Frequency.HOURLY,)
        )
        self.assertEqual(self.coordinator._store.saved, before)
        self.queue.async_import_statistics.assert_not_called()

    async def test_hourly_fetch_pads_provider_date_and_splits_today_before_ha_filtering(self):
        raw = [
            reading("2026-10-16T21:01:00+01:00", 100, 0.1),
            # Fixed-CET date 16 belongs to Madrid's summer calendar date 17.
            reading("2026-10-16T23:01:00+01:00", 100.2, 0.2),
            reading("2026-10-29T12:01:00+01:00", 101.3, 0.3),
            reading("2026-10-30T00:01:00+01:00", 101.7, 0.4),
        ]

        def provider_window(start, end, contract, *, frequency):
            return [
                row for row in raw if start <= datetime.fromisoformat(row["datetime"]).date() < end
            ]

        self.coordinator._api.consumptions.side_effect = provider_window
        fetched = await self.coordinator._fetch(
            date(2026, 10, 17), date(2026, 10, 30), Frequency.HOURLY
        )
        self.assertEqual(
            self.coordinator._api.consumptions.call_args_list,
            [
                call(date(2026, 10, 16), date(2026, 10, 23), CONTRACT, frequency="HOURLY"),
                call(date(2026, 10, 23), date(2026, 10, 29), CONTRACT, frequency="HOURLY"),
                call(date(2026, 10, 29), date(2026, 10, 30), CONTRACT, frequency="HOURLY"),
            ],
        )
        self.assertEqual(
            list(sorted(fetched)),
            [
                "2026-10-16T22:00:00+00:00",
                "2026-10-29T11:00:00+00:00",
            ],
        )
        self.assertEqual(fetched["2026-10-16T22:00:00+00:00"]["delta"], 0.2)
        self.assertEqual(fetched["2026-10-29T11:00:00+00:00"]["delta"], 0.3)

    async def test_daily_reference_fetch_has_exact_labels_without_hourly_padding(self):
        await self.coordinator._fetch(date(2026, 10, 17), date(2026, 10, 30), Frequency.DAILY)
        self.assertEqual(
            self.coordinator._api.consumptions.call_args_list,
            [
                call(date(2026, 10, 17), date(2026, 10, 23), CONTRACT, frequency="DAILY"),
                call(date(2026, 10, 24), date(2026, 10, 28), CONTRACT, frequency="DAILY"),
                call(date(2026, 10, 29), date(2026, 10, 29), CONTRACT, frequency="DAILY"),
            ],
        )
