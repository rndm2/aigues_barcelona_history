"""Entry setup owns shared coordinators before concurrent platform setup."""

from __future__ import annotations

import asyncio
import inspect
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_START, Platform
from homeassistant.core import CoreState, HomeAssistant

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
            is_running=False,
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
            self.assertEqual(set(platforms), {Platform.SENSOR, Platform.BUTTON, Platform.DATE})
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
        self.assertEqual(len(buttons), 6)
        for owner in self.entry.runtime_data:
            self.assertEqual(sum(entity.coordinator is owner for entity in sensors), 1)
            self.assertEqual(sum(entity.coordinator is owner for entity in buttons), 3)
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
        self.assertEqual(set(self.hass.data[DOMAIN]), {"_token_lock", "_request_gate", "OTHER"})
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
        self.assertEqual(set(self.hass.data[DOMAIN]), {"_token_lock", "_request_gate", "OTHER"})
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
        self.assertEqual(set(self.hass.data[DOMAIN]), {"_token_lock", "_request_gate", "OTHER"})
        self.assert_other_integrations_unchanged()


class HistoryStartupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Use HA's real event bus and entry-owned task cleanup for startup refreshes."""

    async def asyncSetUp(self):
        self.hass = HomeAssistant("/tmp/aigues-history-startup-tests")
        self.addCleanup(self.hass.import_executor.shutdown)
        self.entry = ConfigEntry(
            version=2,
            minor_version=1,
            domain=DOMAIN,
            title="History startup test",
            data={},
            options={},
            source="user",
            unique_id=None,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        self.addAsyncCleanup(self.entry._async_process_on_unload, self.hass)
        self.coordinators = tuple(SimpleNamespace(async_refresh=AsyncMock()) for _ in range(2))
        self.entry.runtime_data = self.coordinators
        self.meters = []
        self.enterContext(
            patch.object(
                sensor,
                "AiguesHistoryMeter",
                side_effect=lambda owner: SimpleNamespace(coordinator=owner),
            )
        )
        self.create_task = self.enterContext(
            patch.object(
                self.entry,
                "async_create_background_task",
                wraps=self.entry.async_create_background_task,
            )
        )

    async def setup_sensor(self):
        await sensor.async_setup_entry(self.hass, self.entry, self.meters.extend)

    async def fire_start(self):
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
        await self.hass.async_block_till_done(wait_background_tasks=True)

    def assert_refresh_count(self, count):
        self.assertEqual(self.create_task.call_count, count)
        for owner in self.coordinators:
            self.assertEqual(owner.async_refresh.await_count, count)

    async def test_start_event_then_unload_does_not_remove_consumed_listener(self):
        await self.setup_sensor()
        self.assertEqual(self.hass.bus.async_listeners()[EVENT_HOMEASSISTANT_START], 1)
        self.assert_refresh_count(0)
        with self.assertNoLogs("homeassistant.core", level="ERROR"):
            await self.fire_start()
            await self.fire_start()
            await self.entry._async_process_on_unload(self.hass)
        self.assert_refresh_count(1)
        self.assertNotIn(EVENT_HOMEASSISTANT_START, self.hass.bus.async_listeners())

    async def test_unload_before_start_removes_listener_without_refresh(self):
        await self.setup_sensor()
        with self.assertNoLogs("homeassistant.core", level="ERROR"):
            await self.entry._async_process_on_unload(self.hass)
            await self.entry._async_process_on_unload(self.hass)
            await self.fire_start()
        self.assert_refresh_count(0)
        self.assertNotIn(EVENT_HOMEASSISTANT_START, self.hass.bus.async_listeners())

    async def test_reload_after_start_creates_only_one_new_refresh(self):
        await self.setup_sensor()
        await self.fire_start()
        self.hass.set_state(CoreState.running)
        with self.assertNoLogs("homeassistant.core", level="ERROR"):
            await self.entry._async_process_on_unload(self.hass)
            await self.setup_sensor()
            await self.hass.async_block_till_done(wait_background_tasks=True)
            await self.fire_start()
            await self.entry._async_process_on_unload(self.hass)
        self.assert_refresh_count(2)
        self.assertNotIn(EVENT_HOMEASSISTANT_START, self.hass.bus.async_listeners())

    async def test_setup_while_running_refreshes_once_without_start_listener(self):
        self.hass.set_state(CoreState.running)
        await self.setup_sensor()
        await self.hass.async_block_till_done(wait_background_tasks=True)
        await self.fire_start()
        with self.assertNoLogs("homeassistant.core", level="ERROR"):
            await self.entry._async_process_on_unload(self.hass)
        self.assert_refresh_count(1)
        self.assertNotIn(EVENT_HOMEASSISTANT_START, self.hass.bus.async_listeners())

    async def test_setup_while_starting_does_not_wait_for_another_start_event(self):
        self.hass.set_state(CoreState.starting)
        await self.setup_sensor()
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assert_refresh_count(1)
        self.assertNotIn(EVENT_HOMEASSISTANT_START, self.hass.bus.async_listeners())

    async def test_unload_cancels_the_entry_owned_startup_refresh(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def wait_for_provider():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.coordinators[0].async_refresh.side_effect = wait_for_provider
        await self.setup_sensor()
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_START)
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertEqual(len(self.entry._background_tasks), 1)
        pending = next(iter(self.entry._background_tasks))
        with self.assertNoLogs("homeassistant.core", level="ERROR"):
            await self.entry._async_process_on_unload(self.hass)
            await self.fire_start()
        self.assertTrue(pending.cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.entry._background_tasks)
        self.assertEqual(self.create_task.call_count, 1)
        self.coordinators[0].async_refresh.assert_awaited_once()
        self.coordinators[1].async_refresh.assert_not_awaited()
