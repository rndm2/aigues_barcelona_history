"""Date-range controls preserve provider dates and shared task ownership offline."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.config_validation import custom_serializer
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import probatio
import voluptuous as vol
import yaml

from custom_components import aigues_barcelona_history as integration
from custom_components.aigues_barcelona_history import button, sensor, service
from custom_components.aigues_barcelona_history import date as date_platform
from custom_components.aigues_barcelona_history.const import CONF_CONTRACT, DOMAIN

NOW = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
TODAY = NOW.date()


def coordinator(contract="one", entry_id="history-entry"):
    return SimpleNamespace(
        id=contract,
        contract=contract.upper(),
        entry_id=entry_id,
        _stopping=False,
        _shutdown_requested=False,
        import_in_progress=False,
        last_update_success=False,
        hourly_import_start=TODAY - timedelta(days=7),
        hourly_import_end=TODAY,
        device_info={"identifiers": {(DOMAIN, contract)}},
        async_import_configured_history=AsyncMock(),
        async_import_hourly_date_range=AsyncMock(),
        import_hourly_date_range=AsyncMock(),
        import_configured_consumptions=AsyncMock(),
        import_old_consumptions=AsyncMock(),
        _set_manual_import_reserved=Mock(),
    )


class HourlyDateControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(date_platform.dt_util, "now", return_value=NOW))

    async def test_owned_contracts_get_two_distinct_restorable_date_selectors(self):
        owners = (coordinator("one"), coordinator("two"))
        entities = []
        await date_platform.async_setup_entry(
            SimpleNamespace(data={DOMAIN: {"other": coordinator("other")}}),
            SimpleNamespace(runtime_data=owners),
            entities.extend,
        )
        self.assertIn(Platform.DATE, integration.PLATFORMS)
        self.assertEqual(len(entities), 4)
        self.assertEqual(len({entity.unique_id for entity in entities}), 4)
        self.assertEqual({entity.coordinator.id for entity in entities}, {"one", "two"})
        self.assertEqual(
            {entity.entity_id for entity in entities},
            {
                f"date.{DOMAIN}_{owner.id}_hourly_import_{boundary}"
                for owner in owners
                for boundary in ("start", "end")
            },
        )
        for entity in entities:
            self.assertIsInstance(entity, RestoreEntity)
            self.assertIs(entity.device_info, entity.coordinator.device_info)
            self.assertIs(entity.entity_category, EntityCategory.CONFIG)
            self.assertTrue(entity.has_entity_name)
            self.assertEqual(
                entity.extra_state_attributes["Date range"],
                "Aigües reporting dates, both included",
            )
        self.assertEqual(entities[0].native_value, TODAY - timedelta(days=7))
        self.assertEqual(entities[1].native_value, TODAY)
        self.assertEqual(entities[0].name, "Hourly import from")
        self.assertEqual(entities[1].name, "Hourly import to")

    async def test_empty_runtime_creates_no_date_selectors(self):
        entities = []
        await date_platform.async_setup_entry(
            None, SimpleNamespace(runtime_data=()), entities.extend
        )
        self.assertEqual(entities, [])

    async def test_date_and_button_recovery_availability_share_lifecycle_rules(self):
        owner = coordinator()
        controls = (
            date_platform.HistoryImportDate(owner, date_platform.IMPORT_DATES[0]),
            button.HistoryImportButton(owner, button.HOURLY_RANGE_BUTTON),
        )
        for control in controls:
            self.assertTrue(control.available)
        for attribute in ("_stopping", "_shutdown_requested"):
            setattr(owner, attribute, True)
            for control in controls:
                with self.subTest(attribute=attribute, entity=control.entity_id):
                    self.assertFalse(control.available)
            setattr(owner, attribute, False)
        for control in controls:
            self.assertTrue(control.available)
        owner.import_in_progress = True
        self.assertTrue(controls[0].available)
        self.assertFalse(controls[1].available)

    async def test_individual_date_can_cross_other_selection_without_starting_import(self):
        owner = coordinator()
        entity = date_platform.HistoryImportDate(owner, date_platform.IMPORT_DATES[1])
        entity.async_write_ha_state = Mock()
        selected = owner.hourly_import_start - timedelta(days=1)
        await entity.async_set_value(selected)
        self.assertEqual(entity.native_value, selected)
        self.assertEqual(owner.hourly_import_start, TODAY - timedelta(days=7))
        entity.async_write_ha_state.assert_called_once_with()
        owner.import_hourly_date_range.assert_not_called()
        owner.async_import_hourly_date_range.assert_not_called()

    async def test_invalid_individual_dates_preserve_selection_and_do_not_write(self):
        entity = date_platform.HistoryImportDate(coordinator(), date_platform.IMPORT_DATES[0])
        entity.async_write_ha_state = Mock()
        before = entity.native_value
        for invalid in (TODAY + timedelta(days=1), NOW, "2026-1-01", "unavailable", None, 1):
            with self.subTest(value=invalid):
                with self.assertRaises(HomeAssistantError):
                    await entity.async_set_value(invalid)
                self.assertEqual(entity.native_value, before)
        entity.async_write_ha_state.assert_not_called()

    async def test_provider_today_uses_fixed_cet_at_madrid_summer_midnight(self):
        entity = date_platform.HistoryImportDate(coordinator(), date_platform.IMPORT_DATES[1])
        entity.async_write_ha_state = Mock()
        # Madrid is already Oct 10, while the provider's fixed-CET date is Oct 9.
        with patch.object(
            date_platform.dt_util,
            "now",
            return_value=datetime(2026, 10, 9, 22, 30, tzinfo=timezone.utc),
        ):
            with self.assertRaises(HomeAssistantError):
                await entity.async_set_value(date(2026, 10, 10))
            await entity.async_set_value(date(2026, 10, 9))
        self.assertEqual(entity.native_value, date(2026, 10, 9))

    async def test_restore_valid_selection_uses_same_individual_validation(self):
        owner = coordinator()
        entity = date_platform.HistoryImportDate(owner, date_platform.IMPORT_DATES[0])
        with (
            patch.object(CoordinatorEntity, "async_added_to_hass", new_callable=AsyncMock),
            patch.object(
                entity,
                "async_get_last_state",
                AsyncMock(return_value=SimpleNamespace(state="2025-02-03")),
            ),
        ):
            await entity.async_added_to_hass()
        self.assertEqual(entity.native_value, date(2025, 2, 3))
        owner.async_import_hourly_date_range.assert_not_awaited()

    async def test_invalid_or_unavailable_restored_dates_keep_defaults(self):
        for state in (None, "unknown", "unavailable", "2026-02-30", "2026-10-31"):
            with self.subTest(state=state):
                entity = date_platform.HistoryImportDate(
                    coordinator(), date_platform.IMPORT_DATES[0]
                )
                before = entity.native_value
                with (
                    patch.object(CoordinatorEntity, "async_added_to_hass", new_callable=AsyncMock),
                    patch.object(
                        entity,
                        "async_get_last_state",
                        AsyncMock(return_value=SimpleNamespace(state=state) if state else None),
                    ),
                    self.assertLogs(date_platform.__name__, level="WARNING")
                    if state is not None
                    else patch.object(date_platform._LOGGER, "warning"),
                ):
                    await entity.async_added_to_hass()
                self.assertEqual(entity.native_value, before)

    async def test_range_button_delegates_without_changing_configured_buttons(self):
        owner = coordinator()
        entity = button.HistoryImportButton(owner, button.HOURLY_RANGE_BUTTON)
        await entity.async_press()
        owner.async_import_hourly_date_range.assert_awaited_once_with()
        owner.async_import_configured_history.assert_not_awaited()
        self.assertEqual(entity.name, "Import hourly date range")
        self.assertEqual(entity.entity_id, f"button.{DOMAIN}_one_import_hourly_date_range")
        self.assertEqual(
            [description.key for description in button.IMPORT_BUTTONS],
            ["import_hourly_history", "import_daily_reference"],
        )


class HourlyDateServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = coordinator()
        self.tasks = []

        def create_task(hass, coroutine, name):
            task = asyncio.create_task(coroutine, name=name)
            self.tasks.append(task)
            return task

        self.entry = SimpleNamespace(async_create_background_task=Mock(side_effect=create_task))
        self.handlers = {}
        self.schemas = {}

        def register(domain, name, handler, *, schema):
            self.handlers[domain, name] = handler
            self.schemas[domain, name] = schema

        self.hass = SimpleNamespace(
            data={DOMAIN: {"ONE": {"coordinator": self.owner}}},
            config_entries=SimpleNamespace(async_get_entry=Mock(return_value=self.entry)),
            services=SimpleNamespace(
                has_service=lambda domain, name: (domain, name) in self.handlers,
                async_register=Mock(side_effect=register),
            ),
        )

    def test_required_dates_have_native_date_selectors_in_actual_ha_schema(self):
        fields = probatio.to_field_list(
            service.HOURLY_DATE_RANGE_SCHEMA, custom_serializer=custom_serializer
        )
        dates = {field["name"]: field for field in fields if field["name"] != CONF_CONTRACT}
        self.assertEqual(set(dates), {"start_date", "end_date"})
        for field in dates.values():
            self.assertEqual(field["selector"], {"date": {}})
            self.assertTrue(field["required"])
        request = {"start_date": "2026-10-09", "end_date": "2026-10-10"}
        self.assertEqual(service.HOURLY_DATE_RANGE_SCHEMA(request), request)
        for missing in ("start_date", "end_date"):
            with self.assertRaises(vol.Invalid):
                service.HOURLY_DATE_RANGE_SCHEMA({k: v for k, v in request.items() if k != missing})
        with self.assertRaises(vol.Invalid):
            service.HOURLY_DATE_RANGE_SCHEMA({**request, "start_date": "2026-02-30"})

    async def test_service_routes_only_selected_contract_through_entry_owned_task(self):
        other = coordinator("two", "other-entry")
        self.hass.data[DOMAIN]["TWO"] = {"coordinator": other}
        await service.async_setup(self.hass, {})
        await self.handlers[DOMAIN, service.SERVICE_IMPORT_HOURLY_DATE_RANGE](
            SimpleNamespace(
                data={CONF_CONTRACT: "one", "start_date": "2026-10-09", "end_date": "2026-10-10"}
            )
        )
        self.owner.import_hourly_date_range.assert_awaited_once_with("2026-10-09", "2026-10-10")
        self.assertEqual(len(self.tasks), 1)
        self.assertTrue(self.tasks[0].get_name().endswith("ONE HOURLY"))
        self.assertEqual(
            self.owner._set_manual_import_reserved.call_args_list, [call(True), call(False)]
        )
        self.owner.import_old_consumptions.assert_not_called()
        self.owner.import_configured_consumptions.assert_not_called()
        other.import_hourly_date_range.assert_not_called()

    async def test_ambiguous_contract_and_missing_entry_do_not_reserve_import(self):
        self.hass.data[DOMAIN]["TWO"] = {"coordinator": coordinator("two")}
        await service.async_setup(self.hass, {})
        handler = self.handlers[DOMAIN, service.SERVICE_IMPORT_HOURLY_DATE_RANGE]
        with self.assertRaises(HomeAssistantError):
            await handler(SimpleNamespace(data={"start_date": TODAY, "end_date": TODAY}))
        self.hass.config_entries.async_get_entry.return_value = None
        with self.assertRaises(HomeAssistantError):
            await handler(
                SimpleNamespace(data={CONF_CONTRACT: "ONE", "start_date": TODAY, "end_date": TODAY})
            )
        self.owner._set_manual_import_reserved.assert_not_called()
        self.owner.import_hourly_date_range.assert_not_called()

    async def test_invalid_range_reaches_shared_validator_and_releases_reservation(self):
        self.owner.import_hourly_date_range.side_effect = ValueError("Start date follows end date")
        with self.assertRaisesRegex(ValueError, "Start date follows end date"):
            await service.async_run_import(
                self.hass, self.owner, start_date=TODAY, end_date=TODAY - timedelta(days=1)
            )
        self.owner.import_hourly_date_range.assert_awaited_once_with(
            TODAY, TODAY - timedelta(days=1)
        )
        self.assertEqual(self.owner._set_manual_import_reserved.call_args_list[-1].args, (False,))

    async def test_cancellation_reaches_range_task_and_releases_manual_reservation(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def pending(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.owner.import_hourly_date_range.side_effect = pending
        caller = asyncio.create_task(
            service.async_run_import(self.hass, self.owner, start_date=TODAY, end_date=TODAY)
        )
        await started.wait()
        self.tasks[0].cancel("entry unloaded")
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.owner._set_manual_import_reserved.call_args_list[-1].args, (False,))

    async def test_busy_selection_is_restorable_without_changing_running_range(self):
        started, finish = asyncio.Event(), asyncio.Event()
        captured = []
        self.owner.hass = self.hass
        original = self.owner.hourly_import_start, self.owner.hourly_import_end

        async def pending(start, end):
            captured.append((start, end))
            self.owner.import_in_progress = True
            started.set()
            try:
                await finish.wait()
            finally:
                self.owner.import_in_progress = False

        self.owner.import_hourly_date_range.side_effect = pending
        caller = asyncio.create_task(
            sensor.AiguesHistoryCoordinator.async_import_hourly_date_range(self.owner)
        )
        await started.wait()
        try:
            control = date_platform.HistoryImportDate(self.owner, date_platform.IMPORT_DATES[0])
            control.async_write_ha_state = Mock()
            selected = TODAY - timedelta(days=14)
            with patch.object(date_platform.dt_util, "now", return_value=NOW):
                await control.async_set_value(selected)
                self.assertTrue(control.available)
                self.assertEqual(control.state, selected.isoformat())
                restored = date_platform.HistoryImportDate(
                    coordinator(), date_platform.IMPORT_DATES[0]
                )
                with (
                    patch.object(CoordinatorEntity, "async_added_to_hass", new_callable=AsyncMock),
                    patch.object(
                        restored,
                        "async_get_last_state",
                        AsyncMock(return_value=SimpleNamespace(state=control.state)),
                    ),
                ):
                    await restored.async_added_to_hass()
            self.assertEqual(restored.native_value, selected)
            self.assertEqual(captured, [original])
            self.owner.import_hourly_date_range.assert_awaited_once_with(*original)
        finally:
            finish.set()
            await caller

    async def test_rejected_task_creation_closes_coroutine_and_releases_reservation(self):
        self.entry.async_create_background_task.side_effect = RuntimeError("Entry unloading")
        with self.assertRaisesRegex(RuntimeError, "Entry unloading"):
            await service.async_run_import(self.hass, self.owner, start_date=TODAY, end_date=TODAY)
        created = self.entry.async_create_background_task.call_args.args[1]
        self.assertIsNone(created.cr_frame)
        self.assertEqual(self.owner._set_manual_import_reserved.call_args_list[-1].args, (False,))

    async def test_half_range_or_mixed_import_depth_is_rejected_before_reserving(self):
        for kwargs in (
            {"start_date": TODAY},
            {"end_date": TODAY},
            {"start_date": TODAY, "end_date": TODAY, "days": 7},
            {"start_date": TODAY, "end_date": TODAY, "frequency": "DAILY"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(HomeAssistantError):
                await service.async_run_import(self.hass, self.owner, **kwargs)
        self.owner._set_manual_import_reserved.assert_not_called()
        self.owner.import_hourly_date_range.assert_not_called()

    async def test_service_registration_is_idempotent_and_preserves_old_handlers(self):
        legacy = AsyncMock()
        self.handlers[DOMAIN, service.SERVICE_IMPORT_HISTORICAL_DATA] = legacy
        await service.async_setup(self.hass, {})
        await service.async_setup(self.hass, {})
        self.assertIs(self.handlers[DOMAIN, service.SERVICE_IMPORT_HISTORICAL_DATA], legacy)
        self.assertEqual(self.hass.services.async_register.call_count, 2)

    def test_service_documentation_explains_inclusive_provider_dates(self):
        document = yaml.safe_load(Path(service.__file__).with_name("services.yaml").read_text())
        definition = document[service.SERVICE_IMPORT_HOURLY_DATE_RANGE]
        self.assertIn("Aigües reporting dates, both included", definition["description"])
        self.assertEqual(set(definition["fields"]), {CONF_CONTRACT, "start_date", "end_date"})
        for field in ("start_date", "end_date"):
            self.assertTrue(definition["fields"][field]["required"])
            self.assertEqual(definition["fields"][field]["selector"], {"date": None})


if __name__ == "__main__":
    unittest.main()
