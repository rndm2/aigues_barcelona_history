"""History buttons delegate import behavior and retain isolated identities."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from custom_components.aigues_barcelona_history import button
from custom_components.aigues_barcelona_history.const import DOMAIN


def coordinator(contract="test123", entry_id="history-entry"):
    return SimpleNamespace(
        id=contract,
        entry_id=entry_id,
        _stopping=False,
        _shutdown_requested=False,
        last_update_success=False,
        device_info={"identifiers": {(DOMAIN, contract)}, "name": f"History {contract}"},
        async_import_configured_history=AsyncMock(),
    )


class HistoryImportButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_creates_two_buttons_per_owned_contract_only(self):
        owned_one = coordinator("one")
        owned_two = coordinator("two")
        another = coordinator("other", "another-entry")
        old_data = {"ONE": {"coordinator": object()}}
        hass = SimpleNamespace(
            data={
                "aigues_barcelona": old_data,
                DOMAIN: {
                    "ONE": {"coordinator": owned_one},
                    "TWO": {"coordinator": owned_two},
                    "OTHER": {"coordinator": another},
                    "_token_lock": asyncio.Lock(),
                },
            }
        )
        entities = []
        await button.async_setup_entry(
            hass,
            SimpleNamespace(entry_id="history-entry", runtime_data=(owned_one, owned_two)),
            entities.extend,
        )
        self.assertEqual(len(entities), 4)
        self.assertEqual({entity.coordinator.id for entity in entities}, {"one", "two"})
        self.assertEqual(len({entity.unique_id for entity in entities}), 4)
        self.assertTrue(
            all(entity.entity_id.startswith(f"button.{DOMAIN}_") for entity in entities)
        )
        self.assertIs(hass.data["aigues_barcelona"], old_data)
        for owner in (owned_one, owned_two, another):
            owner.async_import_configured_history.assert_not_awaited()

    async def test_each_button_awaits_only_its_frequency_without_caching_depth(self):
        owner = coordinator()
        for description in button.IMPORT_BUTTONS:
            entity = button.HistoryImportButton(owner, description)
            owner.async_import_configured_history.reset_mock()
            await entity.async_press()
            owner.async_import_configured_history.assert_awaited_once_with(
                description.frequency.value
            )
            self.assertIs(entity.device_info, owner.device_info)
            self.assertIs(entity.entity_category, EntityCategory.CONFIG)
            self.assertTrue(entity.has_entity_name)

    async def test_failed_refresh_does_not_disable_manual_recovery(self):
        owner = coordinator()
        entity = button.HistoryImportButton(owner, button.IMPORT_BUTTONS[0])
        self.assertFalse(owner.last_update_success)
        self.assertTrue(entity.available)
        owner._stopping = True
        self.assertFalse(entity.available)
        owner._stopping = False
        owner._shutdown_requested = True
        self.assertFalse(entity.available)

    async def test_provider_failure_propagates_to_the_pressed_action(self):
        owner = coordinator()
        owner.async_import_configured_history.side_effect = HomeAssistantError(
            "Provider unavailable"
        )
        entity = button.HistoryImportButton(owner, button.IMPORT_BUTTONS[0])
        with self.assertRaisesRegex(HomeAssistantError, "Provider unavailable"):
            await entity.async_press()

    async def test_cancellation_reaches_the_entry_owned_import(self):
        owner = coordinator()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def pending_import(frequency):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        owner.async_import_configured_history.side_effect = pending_import
        entity = button.HistoryImportButton(owner, button.IMPORT_BUTTONS[0])
        task = asyncio.create_task(entity.async_press())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())

    async def test_empty_runtime_does_not_scan_or_publish_another_entry(self):
        add_entities = Mock()
        await button.async_setup_entry(
            SimpleNamespace(data={DOMAIN: {"OTHER": {"coordinator": coordinator()}}}),
            SimpleNamespace(entry_id="empty-entry", runtime_data=()),
            add_entities,
        )
        self.assertEqual(list(add_entities.call_args.args[0]), [])
