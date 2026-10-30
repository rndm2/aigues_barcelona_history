"""Configured import depths share atomic persistence and entry-owned tasks."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, Mock

from test_aigues_barcelona_history import MADRID, OfflineTests, make_coordinator, reading

from custom_components.aigues_barcelona_history.api import AiguesApiServerError
from custom_components.aigues_barcelona_history.const import (
    CONF_HISTORY_DAYS,
    CONF_HOURLY_HISTORY_DAYS,
    CONF_SHOULD_IMPORT_HISTORY,
)
from custom_components.aigues_barcelona_history.settings import effective_settings
from custom_components.aigues_barcelona_history.statistics import (
    Frequency,
    normalize_readings,
)


class ConfiguredImportTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.entry = self.coordinator._entry()
        self.entry.options = {
            CONF_HOURLY_HISTORY_DAYS: 2,
            CONF_HISTORY_DAYS: 20,
            CONF_SHOULD_IMPORT_HISTORY: True,
        }
        self.entry.runtime_data = (self.coordinator,)
        self.coordinator._should_import_history = True
        self.tasks = []

        def create_background(hass, coroutine, name):
            task = asyncio.create_task(coroutine)
            self.tasks.append(task)
            return task

        self.entry.async_create_background_task = Mock(side_effect=create_background)

    async def test_configured_depths_fetch_distinct_ranges_in_one_batch(self):
        self.coordinator._fetch = AsyncMock(return_value={})
        await self.coordinator.import_configured_consumptions(clear_startup_request=True)
        self.assertEqual(
            [item.args for item in self.coordinator._fetch.await_args_list],
            [
                (date(2026, 10, 27), date(2026, 10, 30), Frequency.HOURLY),
                (date(2026, 10, 9), date(2026, 10, 30), Frequency.DAILY),
            ],
        )
        self.coordinator._store.async_save.assert_awaited_once()
        self.coordinator._ensure_token.assert_not_awaited()
        self.assertFalse(self.coordinator._should_import_history)
        self.assertFalse(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])

    async def test_later_frequency_failure_preserves_the_atomic_batch_and_pending_request(self):
        hourly = normalize_readings(
            [reading("2026-10-28T10:01:00+01:00", 100, 0.1)],
            Frequency.HOURLY,
            MADRID,
        )
        self.coordinator._fetch = AsyncMock(
            side_effect=[
                hourly,
                AiguesApiServerError("Daily request unavailable"),
            ]
        )
        with self.assertRaises(AiguesApiServerError):
            await self.coordinator.import_configured_consumptions(clear_startup_request=True)
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()
        self.assertEqual(self.coordinator._ledger, {"DAILY": {}, "HOURLY": {}})
        self.assertTrue(self.coordinator._should_import_history)
        self.assertTrue(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()

    async def test_button_import_uses_current_depth_and_retains_unrelated_startup_request(self):
        self.coordinator._fetch = AsyncMock(return_value={})
        await self.coordinator.async_import_configured_history("HOURLY")
        self.assertEqual(
            self.coordinator._fetch.await_args.args,
            (date(2026, 10, 27), date(2026, 10, 30), Frequency.HOURLY),
        )
        self.entry.options[CONF_HOURLY_HISTORY_DAYS] = 4
        self.coordinator._fetch.reset_mock()
        await self.coordinator.async_import_configured_history("HOURLY")
        self.assertEqual(
            self.coordinator._fetch.await_args.args,
            (date(2026, 10, 25), date(2026, 10, 30), Frequency.HOURLY),
        )
        self.assertEqual(self.entry.async_create_background_task.call_count, 2)
        self.assertTrue(self.coordinator._should_import_history)
        self.assertTrue(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()

    async def test_daily_button_does_not_fetch_hourly_history_or_clear_startup(self):
        self.coordinator._fetch = AsyncMock(return_value={})
        await self.coordinator.async_import_configured_history("DAILY")
        self.assertEqual(
            self.coordinator._fetch.await_args.args,
            (date(2026, 10, 9), date(2026, 10, 30), Frequency.DAILY),
        )
        self.assertTrue(self.coordinator._should_import_history)
        self.assertTrue(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])

    async def test_unload_cancels_the_entry_owned_button_import_without_saving(self):
        started = asyncio.Event()

        async def pending_fetch(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.coordinator._fetch = AsyncMock(side_effect=pending_fetch)
        request = asyncio.create_task(self.coordinator.async_import_configured_history("HOURLY"))
        await started.wait()
        self.assertEqual(len(self.tasks), 1)
        self.tasks[0].cancel("Entry unloaded")
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.coordinator._store.async_save.assert_not_awaited()
        self.assertTrue(self.coordinator._should_import_history)
        self.assertTrue(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])

    async def test_entry_request_clears_only_after_all_contracts_finish(self):
        second = make_coordinator()
        second.hass = self.coordinator.hass
        second.contract, second.id = "SECOND", "second"
        second._should_import_history = True
        self.entry.runtime_data = (self.coordinator, second)
        self.coordinator._import_ranges = AsyncMock()
        second._import_ranges = AsyncMock()
        await self.coordinator.import_configured_consumptions(clear_startup_request=True)
        self.assertFalse(self.coordinator._should_import_history)
        self.assertTrue(second._should_import_history)
        self.assertTrue(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()
        await second.import_configured_consumptions(clear_startup_request=True)
        self.assertFalse(second._should_import_history)
        self.assertFalse(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])
        self.coordinator.hass.config_entries.async_update_entry.assert_called_once()

    async def test_legacy_data_request_is_cleared_by_options_override(self):
        self.entry.data[CONF_SHOULD_IMPORT_HISTORY] = True
        self.entry.options.pop(CONF_SHOULD_IMPORT_HISTORY)
        self.coordinator._fetch = AsyncMock(return_value={})
        await self.coordinator.import_configured_consumptions(clear_startup_request=True)
        self.assertTrue(self.entry.data[CONF_SHOULD_IMPORT_HISTORY])
        self.assertFalse(self.entry.options[CONF_SHOULD_IMPORT_HISTORY])
        self.assertFalse(effective_settings(self.entry)[CONF_SHOULD_IMPORT_HISTORY])
        self.assertEqual(self.entry.options[CONF_HOURLY_HISTORY_DAYS], 2)
        self.assertEqual(self.entry.options[CONF_HISTORY_DAYS], 20)
