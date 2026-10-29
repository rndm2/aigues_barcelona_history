"""Entry setup owns shared coordinators before concurrent platform setup."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import Platform
from homeassistant.core import CoreState

from custom_components import aigues_barcelona_history as integration
from custom_components.aigues_barcelona_history import button, sensor
from custom_components.aigues_barcelona_history.const import (
    CONF_CONTRACT,
    CONF_HISTORY_DAYS,
    CONF_HOURLY_HISTORY_DAYS,
    CONF_SCAN_PERIOD,
    CONF_SHOULD_IMPORT_HISTORY,
    DOMAIN,
)


class HistoryPlatformSetupTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual entry and platform setup without provider access."""

    def setUp(self):
        self.unload_callbacks = []
        self.entry = SimpleNamespace(
            entry_id="history-entry",
            data={
                "username": "unused",
                "password": "unused",
                "token": "stored-token",
                CONF_CONTRACT: ["one", "TWO", "ONE", "two"],
            },
            options={
                CONF_HISTORY_DAYS: 23,
                CONF_HOURLY_HISTORY_DAYS: 11,
                CONF_SCAN_PERIOD: 3900,
                CONF_SHOULD_IMPORT_HISTORY: False,
            },
            async_on_unload=self.unload_callbacks.append,
            async_create_background_task=Mock(),
        )
        self.other = SimpleNamespace(entry_id="another-entry")
        self.shared_lock = asyncio.Lock()
        self.old_store = {"sentinel": object()}
        self.other_data = {"coordinator": self.other}
        self.entries = SimpleNamespace(
            async_entries=Mock(return_value=[self.entry]),
            async_get_entry=Mock(return_value=self.entry),
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )
        self.hass = SimpleNamespace(
            data={
                "aigues_barcelona": self.old_store,
                DOMAIN: {"_token_lock": self.shared_lock, "OTHER": self.other_data},
            },
            config_entries=self.entries,
            state=CoreState.not_running,
            bus=SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
        )

        def initialize_coordinator(owner, hass, logger, **kwargs):
            owner.hass = hass
            owner.config_entry = kwargs["config_entry"]
            owner.last_update_success = True
            owner._shutdown_requested = False

        patches = (
            patch.object(sensor.DataUpdateCoordinator, "__init__", initialize_coordinator),
            patch.object(sensor, "Store"),
            patch.object(sensor, "AiguesBarcelonaHistoryApiClient"),
            patch.object(sensor.AiguesHistoryCoordinator, "async_refresh", new_callable=AsyncMock),
            patch.object(integration, "setup_service", new_callable=AsyncMock),
        )
        self.mocks = [self.enterContext(item) for item in patches]
        self.refresh = self.mocks[3]

    async def run_unload_callbacks(self):
        """Match HA running registered callbacks after failed entry setup."""
        for callback in reversed(self.unload_callbacks):
            result = callback()
            if inspect.isawaitable(result):
                await result

    def assert_other_integrations_unchanged(self):
        self.assertIs(self.hass.data["aigues_barcelona"], self.old_store)
        self.assertIs(self.hass.data[DOMAIN]["OTHER"], self.other_data)
        self.assertIs(self.hass.data[DOMAIN]["_token_lock"], self.shared_lock)

    async def test_button_can_finish_first_with_all_shared_coordinators_ready(self):
        sensors, buttons = [], []

        async def forward(entry, platforms):
            self.assertEqual(set(platforms), {Platform.SENSOR, Platform.BUTTON})
            self.assertIsInstance(entry.runtime_data, tuple)
            self.assertEqual([owner.contract for owner in entry.runtime_data], ["ONE", "TWO"])
            for owner in entry.runtime_data:
                self.assertIs(self.hass.data[DOMAIN][owner.contract]["coordinator"], owner)
                self.assertFalse(owner._should_import_history)
                self.assertIs(owner.config_entry, entry)

            buttons_ready = asyncio.Event()

            async def setup_sensor():
                await buttons_ready.wait()
                await sensor.async_setup_entry(self.hass, entry, sensors.extend)

            async def setup_buttons():
                await button.async_setup_entry(self.hass, entry, buttons.extend)
                buttons_ready.set()

            await asyncio.gather(setup_sensor(), setup_buttons())

        self.entries.async_forward_entry_setups.side_effect = forward
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.assertEqual(len(sensors), 2)
        self.assertEqual(len(buttons), 4)
        for owner in self.entry.runtime_data:
            self.assertEqual(sum(entity.coordinator is owner for entity in sensors), 1)
            self.assertEqual(sum(entity.coordinator is owner for entity in buttons), 2)
        self.assertEqual(len({id(entity.coordinator) for entity in [*sensors, *buttons]}), 2)
        self.refresh.assert_not_awaited()
        self.entry.async_create_background_task.assert_not_called()
        self.hass.bus.async_listen_once.assert_called_once()
        self.assert_other_integrations_unchanged()

    async def test_forward_failure_callback_removes_only_its_own_coordinators(self):
        self.entries.async_forward_entry_setups.side_effect = RuntimeError("platform failed")
        with self.assertRaisesRegex(RuntimeError, "platform failed"):
            await integration.async_setup_entry(self.hass, self.entry)
        self.assertEqual(len(self.entry.runtime_data), 2)
        await self.run_unload_callbacks()
        self.assertFalse(getattr(self.entry, "runtime_data", ()))
        self.assertEqual(set(self.hass.data[DOMAIN]), {"_token_lock", "OTHER"})
        self.assert_other_integrations_unchanged()

    async def test_partial_constructor_failure_is_cleaned_before_platform_forward(self):
        real_coordinator = sensor.AiguesHistoryCoordinator

        def construct(hass, entry, contract):
            if contract == "TWO":
                raise RuntimeError("second contract failed")
            return real_coordinator(hass, entry, contract)

        with patch.object(sensor, "AiguesHistoryCoordinator", side_effect=construct):
            with self.assertRaisesRegex(RuntimeError, "second contract failed"):
                await integration.async_setup_entry(self.hass, self.entry)
        self.entries.async_forward_entry_setups.assert_not_awaited()
        self.assertIn("ONE", self.hass.data[DOMAIN])
        await self.run_unload_callbacks()
        self.assertFalse(getattr(self.entry, "runtime_data", ()))
        self.assertEqual(set(self.hass.data[DOMAIN]), {"_token_lock", "OTHER"})
        self.assert_other_integrations_unchanged()

    async def test_failed_unload_keeps_owners_and_successful_cleanup_is_idempotent(self):
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        owners = self.entry.runtime_data
        self.entries.async_unload_platforms.side_effect = [False, True]
        self.assertFalse(await integration.async_unload_entry(self.hass, self.entry))
        self.assertIs(self.entry.runtime_data, owners)
        for owner in owners:
            self.assertIs(self.hass.data[DOMAIN][owner.contract]["coordinator"], owner)

        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        await self.run_unload_callbacks()
        await self.run_unload_callbacks()
        self.assertFalse(self.entry.runtime_data)
        self.assertEqual(set(self.hass.data[DOMAIN]), {"_token_lock", "OTHER"})
        self.assert_other_integrations_unchanged()
