"""Verify explicit provider-date imports through HA's real SQLite recorder."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from test_aigues_barcelona_history import DAILY_ID, HOURLY_ID, MADRID, NOW, reading
import test_history_energy_runtime as energy_fixture

from custom_components.aigues_barcelona_history.api import (
    AiguesApiAuthError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
)
from custom_components.aigues_barcelona_history.const import (
    CONF_SHOULD_IMPORT_HISTORY,
    MAX_HISTORY_DAYS,
)
from custom_components.aigues_barcelona_history.service import async_run_import
from custom_components.aigues_barcelona_history.statistics import Frequency, normalize_readings


class HourlyRangeRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = energy_fixture.EnergyRecorderTests()
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.setUp()
        self.coordinator = self.runtime.coordinator
        self.runtime.configure(self.coordinator, daily_fallback=False)

    def serve(self, hourly=(), daily=()):
        self.runtime.set_provider_response(list(daily), list(hourly))
        self.coordinator._api.consumptions.reset_mock()

    def assert_intent(self, start, stop):
        self.assertEqual(
            self.coordinator._store.saved["hourly_requested_ranges"],
            [{"start": start.isoformat(), "end": stop.isoformat()}],
        )

    def assert_native_preserved(self):
        self.runtime.assert_native_preserved()

    async def test_inclusive_winter_provider_dates_include_previous_ha_day_first_hour(self):
        first, last = date(2026, 1, 10), date(2026, 1, 11)
        hourly = energy_fixture.provider_hours(first, [1] * 24)
        hourly += energy_fixture.provider_hours(last, [1] * 24)
        self.serve(hourly)

        await self.coordinator.import_hourly_date_range(first, last)

        calls = self.coordinator._api.consumptions.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[:2], (first, last + timedelta(days=1)))
        self.assertEqual(calls[0].kwargs["frequency"], "HOURLY")
        self.assert_intent(first, last + timedelta(days=1))
        rows = self.runtime.fixture.database_rows(HOURLY_ID)
        self.assertEqual(len(rows), 48)
        self.assertEqual(rows[0][0], datetime(2026, 1, 9, 23, tzinfo=MADRID).timestamp())
        self.assertEqual(rows[-1][0], datetime(2026, 1, 11, 22, tzinfo=MADRID).timestamp())
        self.assertAlmostEqual(rows[-1][2], 0.048)
        self.assertEqual(self.runtime.fixture.database_rows(DAILY_ID), [])
        self.assert_native_preserved()

    async def test_iso_dates_and_today_use_seven_day_chunks_with_current_provider_day_separate(
        self,
    ):
        first, last = date(2026, 10, 18), NOW.date()
        self.serve()
        await self.coordinator.import_hourly_date_range(first.isoformat(), last.isoformat())
        self.assertEqual(
            [call.args[:2] for call in self.coordinator._api.consumptions.call_args_list],
            [
                (first, date(2026, 10, 25)),
                (date(2026, 10, 25), last),
                (last, last + timedelta(days=1)),
            ],
        )
        self.assert_intent(first, last + timedelta(days=1))
        self.assertEqual(self.coordinator._ledger, {"DAILY": {}, "HOURLY": {}})

    async def test_requested_intent_is_durable_before_first_authentication_or_get(self):
        day = date(2020, 1, 10)

        async def ensure_token():
            self.assert_intent(day, day + timedelta(days=1))
            self.assertEqual(self.coordinator._store.saved["HOURLY"], {})

        self.coordinator._ensure_token.side_effect = ensure_token
        self.serve()
        await self.coordinator.import_hourly_date_range(day, day)
        self.coordinator._ensure_token.assert_awaited_once()
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "completed")
        self.assert_native_preserved()

    async def test_empty_old_range_without_either_source_survives_restart_and_late_completion(self):
        day = date(2020, 1, 10)
        self.serve()
        await self.coordinator.import_hourly_date_range(day, day)
        self.assert_intent(day, day + timedelta(days=1))
        self.runtime.restart(daily_fallback=False)
        self.coordinator = self.runtime.coordinator
        self.serve(energy_fixture.provider_hours(day, [1] * 24))

        result = await self.coordinator._async_update_data()

        old_calls = [
            call
            for call in self.coordinator._api.consumptions.call_args_list
            if call.args[0] == day
        ]
        self.assertEqual(len(old_calls), 1)
        self.assertEqual(old_calls[0].args[:2], (day, day + timedelta(days=1)))
        self.assertEqual(result["hourly_reconciliation"]["completed_days"], 1)
        self.assertEqual(result["hourly_reconciliation"]["pending_days"], 0)
        self.assertEqual(self.coordinator._store.saved["hourly_backfill_attempts"], {})
        self.assertEqual(len(self.runtime.fixture.database_rows(HOURLY_ID)), 24)
        self.assertAlmostEqual(self.runtime.fixture.database_rows(HOURLY_ID)[-1][2], 0.024)
        self.assert_intent(day, day + timedelta(days=1))
        self.assert_native_preserved()

    async def test_requested_ranges_merge_and_old_empty_days_obey_two_request_budget(self):
        first = date(2020, 1, 10)
        last = first + timedelta(days=2)
        self.serve()
        await self.coordinator.import_hourly_date_range(first, first + timedelta(days=1))
        await self.coordinator.import_hourly_date_range(first + timedelta(days=1), last)
        self.assert_intent(first, last + timedelta(days=1))
        self.coordinator._api.consumptions.reset_mock()

        await self.coordinator._async_update_data()

        old_calls = [
            call
            for call in self.coordinator._api.consumptions.call_args_list
            if first <= call.args[0] <= last
        ]
        self.assertEqual([call.args[0] for call in old_calls], [first, first + timedelta(days=1)])
        self.assertEqual(self.coordinator._data["hourly_reconciliation"]["attempted_requests"], 2)
        self.assertEqual(len(self.coordinator._store.saved["hourly_backfill_attempts"]), 2)
        self.runtime.restart(daily_fallback=False)
        self.coordinator = self.runtime.coordinator
        self.serve()
        await self.coordinator._async_update_data()
        old_calls = [
            call
            for call in self.coordinator._api.consumptions.call_args_list
            if first <= call.args[0] <= last
        ]
        self.assertEqual([call.args[0] for call in old_calls], [last])
        self.assert_native_preserved()

    async def test_manual_provider_scope_updates_own_hours_and_preserves_omitted_and_outside_hours(
        self,
    ):
        day = date(2026, 1, 10)
        before, after = day - timedelta(days=1), day + timedelta(days=1)
        hourly = energy_fixture.provider_hours(before, [1] * 24)
        hourly += energy_fixture.provider_hours(day, [1] * 3)[1:]
        hourly += energy_fixture.provider_hours(after, [1] * 24)
        await self.runtime.import_rows(
            [], hourly, before - timedelta(days=1), after + timedelta(days=2)
        )
        original = deepcopy(self.coordinator._ledger)
        incoming = [
            reading(f"{before}T23:02:00+01:00", 999, 9),
            reading(f"{day}T00:02:00+01:00", 100.001, 0.001),
            reading(f"{day}T01:01:00+01:00", 100.005, 0.004),
            reading(f"{after}T00:02:00+01:00", 999, 9),
        ]
        self.coordinator._api.consumptions.side_effect = None
        self.coordinator._api.consumptions.return_value = incoming

        await self.coordinator.import_hourly_date_range(day, day)

        expected = normalize_readings(incoming[1:3], Frequency.HOURLY, MADRID)
        self.assertEqual(self.coordinator._ledger["HOURLY"], original["HOURLY"] | expected)
        self.assertEqual(self.coordinator._ledger["DAILY"], original["DAILY"])
        self.assert_native_preserved()

    async def test_five_manual_transient_attempts_are_shared_and_intent_survives_final_failure(
        self,
    ):
        day = date(2026, 1, 10)
        self.coordinator._api.consumptions.side_effect = AiguesApiServerError("old report failed")
        with (
            patch.object(self.coordinator, "_wait_for_request", AsyncMock()),
            self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"),
            self.assertRaises(AiguesApiServerError),
        ):
            await self.coordinator.import_hourly_date_range(day, day)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 5)
        self.assert_intent(day, day + timedelta(days=1))
        self.assertEqual(self.coordinator._store.saved["HOURLY"], {})
        self.assertEqual(self.runtime.fixture.database_rows(HOURLY_ID), [])
        self.coordinator._api.login.assert_not_called()
        self.assert_native_preserved()

    async def test_long_provider_pause_stops_manual_retry_and_retains_requested_intent(self):
        day = date(2026, 1, 10)
        self.coordinator._api.consumptions.side_effect = AiguesApiRateLimitError(
            "pause", retry_after=3600
        )
        with (
            self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"),
            self.assertRaises(AiguesApiRateLimitError),
        ):
            await self.coordinator.import_hourly_date_range(day, day)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 1)
        self.assert_intent(day, day + timedelta(days=1))
        self.assertEqual(self.coordinator._store.saved["HOURLY"], {})
        self.assertGreater(self.coordinator._entry().data["retry_not_before"], 0)

    async def test_preflight_auth_failure_keeps_intent_without_source_or_get(self):
        day = date(2026, 1, 10)
        self.coordinator._ensure_token.side_effect = ConfigEntryAuthFailed("Authentication needed")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator.import_hourly_date_range(day, day)
        self.coordinator._api.consumptions.assert_not_called()
        self.assert_intent(day, day + timedelta(days=1))
        self.assertEqual(self.coordinator._store.saved["HOURLY"], {})

    async def test_actual_get_auth_failure_keeps_intent_and_does_not_repeat_login(self):
        day = date(2026, 1, 10)
        self.coordinator._api.consumptions.side_effect = AiguesApiAuthError("invalid token")
        with self.assertRaises((AiguesApiAuthError, ConfigEntryAuthFailed)):
            await self.coordinator.import_hourly_date_range(day, day)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 1)
        self.assert_intent(day, day + timedelta(days=1))
        self.coordinator._api.login.assert_not_called()

    async def test_cancellation_keeps_requested_intent_without_recording_returned_source(self):
        day = date(2026, 1, 10)
        started, release = asyncio.Event(), asyncio.Event()

        async def request(*args, **kwargs):
            started.set()
            await release.wait()
            return energy_fixture.provider_hours(day, [1] * 24)

        self.coordinator._api.consumptions.side_effect = request
        task = asyncio.create_task(self.coordinator.import_hourly_date_range(day, day))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            self.assert_intent(day, day + timedelta(days=1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.coordinator._store.saved["HOURLY"], {})
            self.assertEqual(self.runtime.fixture.database_rows(HOURLY_ID), [])
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assert_native_preserved()

    async def test_invalid_dates_future_and_oversize_fail_before_intent_or_network(self):
        tomorrow = NOW.date() + timedelta(days=1)
        invalid = [
            ("bad", "2026-01-10"),
            (datetime(2026, 1, 10), date(2026, 1, 10)),
            (date(2026, 1, 11), date(2026, 1, 10)),
            (tomorrow, tomorrow),
            (NOW.date() - timedelta(days=MAX_HISTORY_DAYS), NOW.date()),
        ]
        for start, end in invalid:
            with self.subTest(start=start, end=end), self.assertRaises(HomeAssistantError):
                await self.coordinator.import_hourly_date_range(start, end)
        self.coordinator._store.async_save.assert_not_awaited()
        self.coordinator._api.consumptions.assert_not_called()

    async def test_explicit_range_does_not_clear_startup_import_flag(self):
        day = date(2026, 1, 10)
        self.coordinator._should_import_history = True
        self.coordinator._entry().options[CONF_SHOULD_IMPORT_HISTORY] = True
        self.serve()
        await self.coordinator.import_hourly_date_range(day, day)
        self.assertTrue(self.coordinator._should_import_history)
        self.assertTrue(self.coordinator._entry().options[CONF_SHOULD_IMPORT_HISTORY])

    async def test_failure_in_later_window_keeps_entire_intent_and_does_not_partly_commit_source(
        self,
    ):
        first, last = date(2026, 1, 10), date(2026, 1, 17)
        calls = []

        def request(start, end, contract, *, frequency):
            calls.append((start, end))
            if start == first:
                return energy_fixture.provider_hours(first, [1] * 24)
            raise AiguesApiServerError("second window failed")

        self.coordinator._api.consumptions.side_effect = request
        with (
            patch.object(self.coordinator, "_wait_for_request", AsyncMock()),
            self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"),
            self.assertRaises(AiguesApiServerError),
        ):
            await self.coordinator.import_hourly_date_range(first, last)

        self.assert_intent(first, last + timedelta(days=1))
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0], (first, last))
        self.assertEqual(self.coordinator._store.saved["HOURLY"], {})
        self.assertEqual(self.runtime.fixture.database_rows(HOURLY_ID), [])
        self.assert_native_preserved()

    async def test_legacy_import_keeps_ha_civil_scope_and_does_not_create_requested_provider_range(
        self,
    ):
        day = date(2026, 1, 10)
        hourly = energy_fixture.provider_hours(day, [1] * 24)
        hourly += energy_fixture.provider_hours(day + timedelta(days=1), [1] * 24)
        self.serve(hourly)

        await self.coordinator._import_range(day, day + timedelta(days=1), (Frequency.HOURLY,))

        rows = self.runtime.fixture.database_rows(HOURLY_ID)
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0][0], datetime(2026, 1, 10, 0, tzinfo=MADRID).timestamp())
        self.assertEqual(rows[-1][0], datetime(2026, 1, 10, 23, tzinfo=MADRID).timestamp())
        self.assertEqual(self.coordinator._store.saved["hourly_requested_ranges"], [])
        self.assert_native_preserved()

    async def test_intent_storage_failure_prevents_authentication_and_provider_io(self):
        day = date(2026, 1, 10)
        self.coordinator._store.async_save.side_effect = OSError("Store unavailable")
        with self.assertRaises(OSError):
            await self.coordinator.import_hourly_date_range(day, day)
        self.coordinator._ensure_token.assert_not_awaited()
        self.coordinator._api.consumptions.assert_not_called()
        self.assertIsNone(self.coordinator._store.saved)
        self.assertEqual(self.coordinator._hourly_requested_ranges, [])
        self.assert_native_preserved()

    async def test_shared_manual_service_exposes_friendly_range_error_and_releases_reservation(
        self,
    ):
        entry = self.coordinator._entry()
        entry.async_create_background_task = lambda hass, coroutine, name: asyncio.create_task(
            coroutine, name=name
        )
        with self.assertRaisesRegex(HomeAssistantError, "Window end precedes its start"):
            await async_run_import(
                self.coordinator.hass,
                self.coordinator,
                start_date="2026-01-11",
                end_date="2026-01-10",
            )
        self.assertFalse(self.coordinator._manual_import_reserved)
        self.coordinator._store.async_save.assert_not_awaited()
        self.coordinator._ensure_token.assert_not_awaited()
        self.coordinator._api.consumptions.assert_not_called()
        self.assert_native_preserved()


if __name__ == "__main__":
    unittest.main()
