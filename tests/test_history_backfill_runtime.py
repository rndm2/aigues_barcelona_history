"""Exercise delayed-hour reconciliation through HA's actual SQLite recorder."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_aigues_barcelona_history import HOURLY_ID, MADRID, NOW, reading
import test_history_energy_runtime as energy_fixture

from custom_components.aigues_barcelona_history.api import (
    AiguesApiAuthError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
)
from custom_components.aigues_barcelona_history.statistics import Frequency, normalize_readings


class BackfillRecorderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = energy_fixture.EnergyRecorderTests()
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.setUp()
        self.coordinator = self.runtime.coordinator
        self.runtime.configure(self.coordinator, daily_fallback=False)

    async def seed(self, days, *, hourly=None):
        """Persist validated source readings before exercising a normal refresh."""
        daily = [reading(f"{day}T23:02:00+01:00", 100.024, 0.024) for day in days]
        if hourly is None:
            hourly = [energy_fixture.provider_hours(day, [1] * 2)[1] for day in days]
        ledger = {
            "DAILY": normalize_readings(daily, Frequency.DAILY, MADRID),
            "HOURLY": normalize_readings(hourly, Frequency.HOURLY, MADRID),
        }
        await self.coordinator._load_ledger()
        await self.coordinator._save_ledger(ledger)
        self.coordinator._ledger = ledger
        await self.coordinator._publish()
        self.coordinator._update_display()
        self.coordinator._set_import_status(phase="completed")

    def serve(self, *, old=None, fresh=None):
        """Use real request date bounds and override only single old-day GETs."""
        old = old or {}
        self.runtime.set_provider_response([], fresh or [])
        regular = self.coordinator._api.consumptions.side_effect

        def request(start, stop, contract, *, frequency):
            if frequency == "HOURLY" and stop == start + timedelta(days=1) and start in old:
                result = old[start]
                if isinstance(result, Exception):
                    raise result
                return result
            return regular(start, stop, contract, frequency=frequency)

        self.coordinator._api.consumptions.side_effect = request
        self.coordinator._api.consumptions.reset_mock()

    def requests_for(self, days):
        return [
            call
            for call in self.coordinator._api.consumptions.call_args_list
            if call.kwargs["frequency"] == "HOURLY"
            and call.args[0] in days
            and call.args[1] == call.args[0] + timedelta(days=1)
        ]

    def assert_native_preserved(self):
        self.runtime.assert_native_preserved()

    async def test_normal_poll_completes_winter_provider_block_including_previous_civil_hour(self):
        old = date(2026, 1, 10)
        await self.seed([old])
        complete = energy_fixture.provider_hours(old, [1] * 24)
        self.serve(old={old: complete})

        result = await self.coordinator._async_update_data()

        self.assertEqual(result["import_status"]["phase"], "completed")
        self.assertEqual(result["hourly_reconciliation"]["completed_days"], 1)
        self.assertEqual(result["hourly_reconciliation"]["pending_days"], 0)
        self.assertEqual(len(self.requests_for([old])), 1)
        self.assertEqual(self.coordinator._store.saved["hourly_backfill_attempts"], {})
        rows = self.runtime.fixture.database_rows(HOURLY_ID)
        first = datetime(2026, 1, 9, 23, tzinfo=MADRID).timestamp()
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0][0], first)
        self.assertAlmostEqual(rows[-1][2], 0.024)
        self.assertEqual(len(self.runtime.recorder_period(old - timedelta(days=1), old, "hour")), 1)
        await self.coordinator._async_update_data()
        self.assertEqual(len(self.requests_for([old])), 1)
        self.assert_native_preserved()

    async def test_empty_old_responses_keep_readings_and_persist_two_request_fairness_on_restart(
        self,
    ):
        days = [date(2026, 10, day) for day in (9, 10, 11)]
        await self.seed(days)
        original = deepcopy(self.coordinator._ledger)
        self.serve(old=dict.fromkeys(days, []))

        await self.coordinator._async_update_data()

        self.assertEqual([call.args[0] for call in self.requests_for(days)], days[:2])
        self.assertEqual(self.coordinator._ledger, original)
        self.assertEqual(
            self.coordinator._store.saved["hourly_backfill_attempts"],
            {day.isoformat(): NOW.timestamp() for day in days[:2]},
        )
        self.runtime.restart(daily_fallback=False)
        self.coordinator = self.runtime.coordinator
        self.serve(old=dict.fromkeys(days, []))
        await self.coordinator._async_update_data()
        self.assertEqual([call.args[0] for call in self.requests_for(days)], days[2:])
        self.assertEqual(
            set(self.coordinator._hourly_backfill_attempts), {str(day) for day in days}
        )
        self.assertEqual(self.coordinator._ledger, original)
        self.assert_native_preserved()

    async def test_old_server_error_does_not_undo_fresh_hours_or_retry_five_times(self):
        days = [date(2026, 10, day) for day in (9, 10)]
        await self.seed(days)
        fresh = [reading(f"{NOW.date()}T01:02:00+01:00", 101, 0.004)]
        self.serve(
            old={days[0]: AiguesApiServerError("old report failed"), days[1]: []}, fresh=fresh
        )

        with self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"):
            result = await self.coordinator._async_update_data()

        self.assertEqual([call.args[0] for call in self.requests_for(days)], days)
        self.assertEqual(result["import_status"]["phase"], "completed")
        self.assertEqual(result["hourly_reconciliation"]["attempted_requests"], 2)
        self.assertEqual(result["hourly_reconciliation"]["last_error"], "AiguesApiServerError")
        today = self.runtime.recorder_period(NOW.date(), NOW.date() + timedelta(days=1), "day")
        self.assertEqual(round(today[0]["change"]), 4)
        self.assertEqual(len(self.coordinator._hourly_backfill_attempts), 2)
        self.coordinator._api.login.assert_not_called()
        self.assert_native_preserved()

    async def test_preflight_pause_is_not_counted_as_an_attempt(self):
        day = date(2026, 1, 10)
        await self.seed([day])
        before = deepcopy(self.coordinator._store.saved)
        self.coordinator._store.async_save.reset_mock()
        self.coordinator._api.consumptions.reset_mock()
        with (
            patch.object(
                self.coordinator,
                "_wait_for_request",
                AsyncMock(side_effect=AiguesApiRateLimitError("pause", retry_after=3600)),
            ),
            self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"),
        ):
            await self.coordinator._recheck_incomplete_hours()
        self.coordinator._api.consumptions.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()
        self.assertEqual(self.coordinator._store.saved, before)
        self.assertEqual(self.coordinator._data["hourly_reconciliation"]["attempted_requests"], 0)

    async def test_preflight_auth_failure_does_not_mark_or_save_unattempted_day(self):
        day = date(2026, 1, 10)
        await self.seed([day])
        before = deepcopy(self.coordinator._store.saved)
        self.coordinator._store.async_save.reset_mock()
        self.coordinator._api.consumptions.reset_mock()
        self.coordinator._ensure_token.side_effect = ConfigEntryAuthFailed("Authentication needed")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._recheck_incomplete_hours()
        self.coordinator._api.consumptions.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()
        self.assertEqual(self.coordinator._store.saved, before)
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "completed")

    async def test_actual_old_get_auth_failure_reaches_normal_ha_reauthentication(self):
        days = [date(2026, 10, day) for day in (9, 10)]
        await self.seed(days)
        self.serve(old={days[0]: AiguesApiAuthError("invalid token"), days[1]: []})
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._async_update_data()
        self.assertEqual([call.args[0] for call in self.requests_for(days)], days[:1])
        self.assertEqual(self.coordinator._hourly_backfill_attempts, {})
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "completed")
        self.coordinator._api.login.assert_not_called()

    async def test_partial_response_updates_own_hours_retains_missing_and_excludes_other_provider_date(
        self,
    ):
        old = date(2026, 1, 10)
        next_day = old + timedelta(days=1)
        initial = energy_fixture.provider_hours(old, [1] * 3)[1:]
        other = energy_fixture.provider_hours(next_day, [1] * 24)
        await self.seed([old, next_day], hourly=initial + other)
        outside = normalize_readings(other, Frequency.HOURLY, MADRID)
        incoming = [
            reading(f"{old}T00:02:00+01:00", 100.001, 0.001),
            reading(f"{old}T01:01:00+01:00", 100.005, 0.004),
            reading(f"{next_day}T00:02:00+01:00", 999, 9),
        ]
        self.serve(old={old: incoming})

        await self.coordinator._async_update_data()

        self.assertEqual(len(self.requests_for([old, next_day])), 1)
        ledger = self.coordinator._ledger["HOURLY"]
        for key, value in outside.items():
            self.assertEqual(ledger[key], value)
        retained = normalize_readings(initial[1:], Frequency.HOURLY, MADRID)
        for key, value in retained.items():
            self.assertEqual(ledger[key], value)
        correction = normalize_readings(incoming[:2], Frequency.HOURLY, MADRID)
        for key, value in correction.items():
            self.assertEqual(ledger[key], value)
        self.assertEqual(len(ledger), 27)
        self.assertAlmostEqual(self.runtime.fixture.database_rows(HOURLY_ID)[-1][2], 0.030)
        self.assertEqual(set(self.coordinator._hourly_backfill_attempts), {str(old)})
        self.assert_native_preserved()

    async def test_provider_rate_limit_stops_old_budget_after_one_actual_get(self):
        days = [date(2026, 10, day) for day in (9, 10)]
        await self.seed(days)
        self.serve(
            old={days[0]: AiguesApiRateLimitError("slow down", retry_after=3600), days[1]: []}
        )
        with self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"):
            result = await self.coordinator._async_update_data()
        self.assertEqual([call.args[0] for call in self.requests_for(days)], days[:1])
        self.assertEqual(set(self.coordinator._hourly_backfill_attempts), {str(days[0])})
        self.assertEqual(result["import_status"]["phase"], "completed")
        self.assertEqual(result["hourly_reconciliation"]["attempted_requests"], 1)

    async def test_manual_reservation_prevents_background_provider_requests(self):
        day = date(2026, 1, 10)
        await self.seed([day])
        self.coordinator._api.consumptions.reset_mock()
        self.coordinator._store.async_save.reset_mock()
        self.coordinator._manual_import_reserved = True
        await self.coordinator._recheck_incomplete_hours()
        self.coordinator._api.consumptions.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_cancellation_during_old_get_keeps_fresh_commit_without_backfill_source_writes(
        self,
    ):
        day = date(2026, 1, 10)
        await self.seed([day])
        fresh = [reading(f"{NOW.date()}T01:02:00+01:00", 101, 0.004)]
        self.serve(fresh=fresh)
        regular = self.coordinator._api.consumptions.side_effect
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(start, stop, contract, *, frequency):
            if start == day and frequency == "HOURLY":
                started.set()
                await release.wait()
                return energy_fixture.provider_hours(day, [1] * 24)
            return regular(start, stop, contract, frequency=frequency)

        self.coordinator._api.consumptions.side_effect = request
        task = asyncio.create_task(self.coordinator._async_update_data())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            before = deepcopy(self.coordinator._store.saved)
            source_rows = self.runtime.source_rows()
            self.coordinator._store.async_save.reset_mock()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.coordinator._store.async_save.assert_not_awaited()
            self.assertEqual(self.coordinator._store.saved, before)
            self.assertEqual(self.runtime.source_rows(), source_rows)
            self.assertEqual(self.coordinator._hourly_backfill_attempts, {})
            self.assertEqual(self.coordinator._data["import_status"]["phase"], "completed")
            today = self.runtime.recorder_period(NOW.date(), NOW.date() + timedelta(days=1), "day")
            self.assertEqual(round(today[0]["change"]), 4)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assert_native_preserved()

    async def test_invalid_old_response_records_attempt_without_changing_valid_source(self):
        day = date(2026, 1, 10)
        await self.seed([day])
        before = deepcopy(self.coordinator._ledger)
        self.serve(old={day: [reading(f"{day}T02:02:00+01:00", 100, -1)]})
        with self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"):
            await self.coordinator._async_update_data()
        self.assertEqual(self.coordinator._ledger, before)
        self.assertEqual(set(self.coordinator._hourly_backfill_attempts), {str(day)})
        self.assert_native_preserved()

    async def test_unexpected_programmer_error_is_not_silently_deferred(self):
        day = date(2026, 1, 10)
        await self.seed([day])
        self.serve(old={day: RuntimeError("unexpected bug")})
        with self.assertRaisesRegex(UpdateFailed, "RuntimeError"):
            await self.coordinator._async_update_data()
        self.assertEqual(self.coordinator._hourly_backfill_attempts, {})

    async def test_recent_refresh_keeps_its_existing_five_transient_attempts(self):
        attempts = 0

        def request(start, stop, contract, *, frequency):
            nonlocal attempts
            if frequency == "DAILY" and start == NOW.date() - timedelta(days=7):
                attempts += 1
                if attempts < 5:
                    raise AiguesApiServerError("transient current report")
            return []

        self.coordinator._api.consumptions.side_effect = request
        with (
            patch.object(self.coordinator, "_wait_for_request", AsyncMock()),
            self.assertLogs("custom_components.aigues_barcelona_history.sensor", "WARNING"),
        ):
            result = await self.coordinator._async_update_data()
        self.assertEqual(attempts, 5)
        self.assertEqual(result["import_status"]["phase"], "completed")
        self.coordinator._api.login.assert_not_called()

    async def test_partial_before_stable_frontier_is_retried_but_ancient_zero_hour_prefix_is_not(
        self,
    ):
        prefix, partial, frontier = [date(2026, 6, day) for day in (10, 11, 12)]
        hourly = energy_fixture.provider_hours(partial, [1] * 24)[7:]
        hourly += energy_fixture.provider_hours(frontier, [1] * 24)
        await self.seed([prefix, partial, frontier], hourly=hourly)
        self.assertEqual(self.coordinator._energy_hourly_since, frontier)
        self.serve(old={partial: []})

        await self.coordinator._async_update_data()

        self.assertEqual(
            [call.args[0] for call in self.requests_for([prefix, partial, frontier])], [partial]
        )
        self.assertEqual(set(self.coordinator._hourly_backfill_attempts), {str(partial)})
        self.assertEqual(self.coordinator._energy_hourly_since, frontier)
        self.assert_native_preserved()

    async def test_invalid_optional_scheduling_metadata_resets_without_discarding_source_readings(
        self,
    ):
        day = date(2026, 1, 10)
        await self.seed([day])
        original = deepcopy(self.coordinator._ledger)
        self.coordinator._store.saved["hourly_backfill_attempts"] = {str(day): float("inf")}
        self.runtime.restart(daily_fallback=False)
        self.coordinator = self.runtime.coordinator
        self.serve(old={day: []})

        with self.assertLogs(
            "custom_components.aigues_barcelona_history.sensor", "WARNING"
        ) as logs:
            await self.coordinator._async_update_data()

        self.assertTrue(any("Reset invalid saved" in entry for entry in logs.output))
        self.assertEqual(self.coordinator._ledger, original)
        self.assertEqual(self.coordinator._hourly_backfill_attempts, {str(day): NOW.timestamp()})
        self.assert_native_preserved()

    async def test_recorder_drop_is_detected_and_next_refresh_replays_durable_backfill_readings(
        self,
    ):
        day = date(2026, 1, 10)
        await self.seed([day])
        completed = energy_fixture.provider_hours(day, [1] * 24)
        self.serve(old={day: completed})
        queue = self.runtime.fixture.queue
        import_statistics = queue.async_import_statistics.side_effect

        def drop_hourly(metadata, points, table):
            if metadata["statistic_id"] != HOURLY_ID:
                import_statistics(metadata, points, table)

        queue.async_import_statistics.side_effect = drop_hourly
        with self.assertRaisesRegex(UpdateFailed, "complete History batch"):
            await self.coordinator._async_update_data()
        self.assertEqual(len(self.coordinator._store.saved["HOURLY"]), 24)
        self.assertEqual(len(self.runtime.fixture.database_rows(HOURLY_ID)), 1)
        queue.async_import_statistics.side_effect = import_statistics

        await self.coordinator._async_update_data()

        self.assertEqual(len(self.runtime.fixture.database_rows(HOURLY_ID)), 24)
        self.assertAlmostEqual(self.runtime.fixture.database_rows(HOURLY_ID)[-1][2], 0.024)
        self.assertEqual(len(self.requests_for([day])), 1)
        self.assert_native_preserved()


if __name__ == "__main__":
    unittest.main()
