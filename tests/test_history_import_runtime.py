"""Operational import progress, provider pacing and duplicate protection."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
from types import MethodType
from unittest.mock import AsyncMock, Mock, call, patch

from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from test_aigues_barcelona_history import (
    CONF_VALUE,
    MADRID,
    FakeStore,
    OfflineTests,
    make_coordinator,
    reading,
)

from custom_components.aigues_barcelona_history import button, sensor, service
from custom_components.aigues_barcelona_history.api import (
    AiguesApiError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
)
from custom_components.aigues_barcelona_history.statistics import Frequency, normalize_readings

START = date(2026, 10, 20)
END = date(2026, 10, 30)


class SharedRequestPacingTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        self.real_sleep = asyncio.sleep
        self.enterContext(patch.object(sensor, "MIN_REQUEST_INTERVAL_SECONDS", 1))
        self.enterContext(patch.object(sensor, "monotonic", side_effect=lambda: self.clock))

        async def advance(seconds):
            self.clock += seconds
            await self.real_sleep(0)

        self.sleep = self.enterContext(
            patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock, side_effect=advance)
        )

    async def test_concurrent_contracts_share_one_get_and_one_second_after_response(self):
        second = make_coordinator()
        second.hass = self.coordinator.hass
        second._request_gate = self.coordinator._request_gate
        active, maximum = 0, 0
        starts, ends = [], []

        async def response(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            starts.append(self.clock)
            await self.real_sleep(0)
            self.clock += 0.4
            ends.append(self.clock)
            active -= 1
            return []

        for owner in (self.coordinator, second):
            owner._api.consumptions.side_effect = response
        await asyncio.gather(
            self.coordinator._request_consumptions(START, END, Frequency.DAILY),
            second._request_consumptions(START, END, Frequency.HOURLY),
        )
        self.assertEqual(maximum, 1)
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - ends[0], 1)
        self.sleep.assert_awaited_once_with(1)

    async def test_pacing_does_not_add_to_or_replace_existing_retry_backoffs(self):
        self.coordinator._api.consumptions.side_effect = [
            AiguesApiServerError("Unavailable") for _ in range(4)
        ] + [[]]
        await self.coordinator._request_consumptions(START, END, Frequency.HOURLY)
        self.assertEqual(self.sleep.await_args_list, [call(1), call(2), call(4), call(8)])
        self.assertEqual(self.coordinator._api.consumptions.call_count, 5)
        self.assertEqual(self.coordinator._ensure_token.await_count, 5)

    async def test_retry_after_is_not_shortened_or_added_to_the_shared_spacing(self):
        self.coordinator._api.consumptions.side_effect = [
            AiguesApiRateLimitError("Rate limited", retry_after=12),
            [],
        ]
        with patch.object(sensor.time, "time", side_effect=lambda: self.clock):
            await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.sleep.assert_awaited_once_with(12)

    async def test_token_expiring_between_windows_is_renewed_before_the_next_get(self):
        entry = self.coordinator._entry()
        entry.data[CONF_TOKEN] = "old-token"
        self.coordinator._ensure_token = MethodType(
            sensor.AiguesHistoryCoordinator._ensure_token, self.coordinator
        )
        expired = [False]
        self.coordinator._api.is_token_expired.side_effect = lambda: expired[0]

        def login():
            expired[0] = False
            return True

        self.coordinator._api.login.side_effect = login
        self.coordinator._api.get_token.return_value = "renewed-token"
        seen = []

        def response(*args, **kwargs):
            seen.append(entry.data[CONF_TOKEN])
            expired[0] = True
            return []

        self.coordinator._api.consumptions.side_effect = response
        await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.assertEqual(seen, ["old-token", "renewed-token"])
        self.coordinator._api.login.assert_called_once()

    async def test_cancelling_pacing_wait_cannot_send_another_get(self):
        await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        entered = asyncio.Event()

        async def pending_wait(seconds):
            entered.set()
            await asyncio.Event().wait()

        self.sleep.side_effect = pending_wait
        task = asyncio.create_task(
            self.coordinator._request_consumptions(START, END, Frequency.HOURLY)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.coordinator._api.consumptions.assert_called_once()
        self.assertEqual(self.coordinator._ensure_token.await_count, 2)
        self.coordinator._api.login.assert_not_called()
        self.assertFalse(self.coordinator._request_gate["lock"].locked())


class ImportRuntimeStatusTests(OfflineTests):
    async def test_progress_counts_the_exact_planned_requests_for_both_frequencies(self):
        snapshots = []
        self.coordinator.async_update_listeners.side_effect = lambda: snapshots.append(
            deepcopy(self.coordinator._data["import_status"])
        )
        await self.coordinator._import_range(START, END, tuple(Frequency))
        status = self.coordinator._data["import_status"]
        self.assertEqual(
            status,
            {
                "phase": "completed",
                "frequency": "HOURLY",
                "current_window_start": "2026-10-29",
                "current_window_end": "2026-10-30",
                "completed_windows": 6,
                "total_windows": 6,
                "last_error": None,
            },
        )
        self.assertEqual(self.coordinator._api.consumptions.call_count, 6)
        self.assertEqual({row["completed_windows"] for row in snapshots}, set(range(7)))
        self.assertEqual(
            self.coordinator._data["provider_daily_coverage"],
            {
                "first_interval": None,
                "last_interval": None,
                "known_intervals": 0,
            },
        )

    async def test_failure_retains_completed_windows_and_safe_error_class(self):
        private = "PRIVATE_PROVIDER_RESPONSE_AND_TOKEN"
        snapshots = []
        self.coordinator.async_update_listeners.side_effect = lambda: snapshots.append(
            deepcopy(self.coordinator._data["import_status"])
        )
        self.coordinator._api.consumptions.side_effect = [[], AiguesApiError(private)]
        with self.assertRaises(AiguesApiError):
            await self.coordinator._import_range(START, END, (Frequency.DAILY,))
        status = self.coordinator._data["import_status"]
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["completed_windows"], 1)
        self.assertEqual(status["total_windows"], 3)
        self.assertEqual(status["current_window_start"], "2026-10-27")
        self.assertEqual(status["last_error"], "AiguesApiError")
        self.assertNotIn(private, repr(snapshots))
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_retry_phase_is_visible_until_a_real_response_arrives(self):
        self.coordinator._api.consumptions.side_effect = [AiguesApiServerError("Retry"), []]
        seen = []

        async def pause(seconds):
            seen.append(deepcopy(self.coordinator._data["import_status"]))

        with patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock, side_effect=pause):
            await self.coordinator._import_range(
                date(2026, 10, 27), date(2026, 10, 28), (Frequency.DAILY,)
            )
        self.assertEqual(seen[0]["phase"], "retrying")
        self.assertEqual(seen[0]["last_error"], "AiguesApiServerError")
        self.assertEqual(seen[0]["completed_windows"], 0)
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "completed")
        self.assertIsNone(self.coordinator._data["import_status"]["last_error"])

    async def test_unverified_cached_ledger_is_not_exposed_as_a_meter_reading(self):
        daily = normalize_readings(
            [reading("2026-10-27T23:01:00+01:00", 12, 0.1)], Frequency.DAILY, MADRID
        )
        self.coordinator._store = FakeStore({"DAILY": daily, "HOURLY": {}})
        self.coordinator._publish = AsyncMock(side_effect=ValueError("Recorder conflict"))
        with self.assertRaises(ValueError):
            await self.coordinator._import_range(START, END, (Frequency.DAILY,))
        self.assertNotIn(CONF_VALUE, self.coordinator._data)
        self.coordinator.async_set_updated_data.assert_not_called()
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "failed")

    async def test_verified_cached_reading_is_available_during_and_after_manual_failure(self):
        hass = HomeAssistant("/tmp/aigues-history-cached-state-tests")
        self.addCleanup(hass.import_executor.shutdown)
        hass.config_entries = self.coordinator.hass.config_entries
        entry = self.coordinator._entry()
        entry.async_on_unload = Mock()
        DataUpdateCoordinator.__init__(
            self.coordinator, hass, sensor._LOGGER, config_entry=entry, name="History cached test"
        )
        self.addAsyncCleanup(self.coordinator.async_shutdown)
        del self.coordinator.async_set_updated_data
        del self.coordinator.async_update_listeners
        daily = normalize_readings(
            [reading("2026-10-27T23:01:00+01:00", 12, 0.1)], Frequency.DAILY, MADRID
        )
        self.coordinator._store = FakeStore({"DAILY": daily, "HOURLY": {}})
        self.coordinator._publish = AsyncMock()
        self.coordinator.last_update_success = False
        meter = sensor.AiguesHistoryMeter(self.coordinator)
        meter.hass = hass

        def publish_entity():
            state = meter._async_calculate_state()
            hass.states.async_set(meter.entity_id, state.state, state.attributes)

        self.addCleanup(self.coordinator.async_add_listener(publish_entity))
        entered, release = asyncio.Event(), asyncio.Event()

        async def provider_wait(*args):
            entered.set()
            await release.wait()
            raise AiguesApiError("Provider unavailable")

        self.coordinator._request_consumptions = AsyncMock(side_effect=provider_wait)
        task = asyncio.create_task(self.coordinator._import_range(START, END, (Frequency.DAILY,)))
        await entered.wait()
        state = hass.states.get(meter.entity_id)
        self.assertTrue(meter.available)
        self.assertEqual(float(state.state), 12)
        self.assertEqual(state.attributes["import_status"]["phase"], "running")
        self.assertEqual(state.attributes["provider_daily_coverage"]["known_intervals"], 1)
        release.set()
        with self.assertRaises(AiguesApiError):
            await task
        state = hass.states.get(meter.entity_id)
        self.assertEqual(float(state.state), 12)
        self.assertEqual(state.attributes["import_status"]["phase"], "failed")
        self.assertTrue(meter.available)

    async def test_daily_coverage_reports_actual_saved_span_without_filling_holes(self):
        self.coordinator._ledger = {
            "DAILY": normalize_readings(
                [
                    reading("2026-10-28T23:01:00+01:00", 12, 0.1),
                    reading("2026-10-22T23:01:00+01:00", 11, 0.0),
                ],
                Frequency.DAILY,
                MADRID,
            ),
            "HOURLY": {},
        }
        self.coordinator._update_display()
        self.assertEqual(
            self.coordinator._data["provider_daily_coverage"],
            {
                "first_interval": "2026-10-21T22:00:00+00:00",
                "last_interval": "2026-10-27T23:00:00+00:00",
                "known_intervals": 2,
            },
        )

    async def test_failure_to_create_entry_task_releases_reservation_and_closes_coroutine(self):
        entry = self.coordinator._entry()
        entry.async_create_background_task = Mock(side_effect=RuntimeError("Entry stopping"))
        with self.assertRaises(RuntimeError):
            await service.async_run_import(self.coordinator.hass, self.coordinator, days=2)
        coroutine = entry.async_create_background_task.call_args.args[1]
        self.assertIsNone(coroutine.cr_frame)
        self.assertFalse(self.coordinator._manual_import_reserved)

    async def test_two_simultaneous_manual_calls_reject_duplicate_without_cancelling_job(self):
        entry = self.coordinator._entry()
        tasks = []

        def create_task(hass, coroutine, name):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        entry.async_create_background_task = Mock(side_effect=create_task)
        entered, release = asyncio.Event(), asyncio.Event()

        async def provider_wait(*args):
            entered.set()
            await release.wait()
            raise AiguesApiError("Temporary provider failure")

        self.coordinator._request_consumptions = AsyncMock(side_effect=provider_wait)
        entity = button.HistoryImportButton(self.coordinator, button.IMPORT_BUTTONS[0])
        first = asyncio.create_task(
            service.async_run_import(
                self.coordinator.hass, self.coordinator, frequency="HOURLY", days=2
            )
        )
        second = asyncio.create_task(
            service.async_run_import(
                self.coordinator.hass, self.coordinator, frequency="HOURLY", days=2
            )
        )
        await entered.wait()
        with self.assertRaisesRegex(HomeAssistantError, "already running"):
            await second
        self.assertFalse(first.done())
        self.assertEqual(len(tasks), 1)
        self.assertFalse(entity.available)
        release.set()
        with self.assertRaises(AiguesApiError):
            await first
        self.assertFalse(self.coordinator._manual_import_reserved)
        self.assertTrue(entity.available)
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "failed")
        self.coordinator._request_consumptions.side_effect = None
        self.coordinator._request_consumptions.return_value = []
        await service.async_run_import(
            self.coordinator.hass, self.coordinator, frequency="HOURLY", days=2
        )
        self.assertEqual(len(tasks), 2)
        self.assertTrue(entity.available)
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "completed")

    async def test_initial_or_periodic_import_blocks_manual_duplicate_but_failure_allows_retry(
        self,
    ):
        self.coordinator._set_import_status(phase="running")
        entry = self.coordinator._entry()
        entry.async_create_background_task = Mock()
        entity = button.HistoryImportButton(self.coordinator, button.IMPORT_BUTTONS[0])
        self.assertFalse(entity.available)
        with self.assertRaisesRegex(HomeAssistantError, "already running"):
            await service.async_run_import(self.coordinator.hass, self.coordinator)
        entry.async_create_background_task.assert_not_called()
        self.coordinator._set_import_status(phase="failed", last_error="AiguesApiError")
        self.assertTrue(entity.available)

    async def test_cancelled_manual_import_releases_reservation_and_reports_cancellation(self):
        entry = self.coordinator._entry()
        entry.async_create_background_task = lambda hass, coroutine, name: asyncio.create_task(
            coroutine
        )
        entered = asyncio.Event()

        async def provider_wait(*args):
            entered.set()
            await asyncio.Event().wait()

        self.coordinator._request_consumptions = AsyncMock(side_effect=provider_wait)
        task = asyncio.create_task(
            service.async_run_import(
                self.coordinator.hass, self.coordinator, frequency="HOURLY", days=2
            )
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.coordinator._manual_import_reserved)
        self.assertEqual(self.coordinator._data["import_status"]["phase"], "failed")
        self.assertEqual(self.coordinator._data["import_status"]["last_error"], "CancelledError")
        self.coordinator._store.async_save.assert_not_awaited()
