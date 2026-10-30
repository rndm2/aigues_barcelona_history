"""Friendly Energy coverage choices remain strict and separate from sign-in."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.helpers.config_validation import custom_serializer
import probatio
import test_history_entry_safety as entry_fixture
import voluptuous as vol

from custom_components.aigues_barcelona_history import config_flow, settings
from custom_components.aigues_barcelona_history.const import (
    CONF_ENERGY_DAILY_FALLBACK,
    CONF_SHOULD_IMPORT_HISTORY,
    DEFAULT_ENERGY_DAILY_FALLBACK,
)

INVALID_BOOLEANS = (0, 1, 0.0, Decimal(1), "false", "true", "yes", "", [], {}, None)


class EnergySettingsTests(unittest.TestCase):
    def test_new_and_legacy_entries_default_to_daily_fallback_without_mutation(self):
        entry = SimpleNamespace(data={"token": "retained"}, options={})
        before = deepcopy((entry.data, entry.options))
        self.assertIs(DEFAULT_ENERGY_DAILY_FALLBACK, True)
        self.assertIs(settings.new_entry_settings()[CONF_ENERGY_DAILY_FALLBACK], True)
        self.assertIs(settings.effective_settings(entry)[CONF_ENERGY_DAILY_FALLBACK], True)
        self.assertEqual((entry.data, entry.options), before)

    def test_explicit_false_survives_legacy_data_options_and_form_round_trip(self):
        for data, options in (
            ({CONF_ENERGY_DAILY_FALLBACK: False}, {}),
            ({CONF_ENERGY_DAILY_FALLBACK: True}, {CONF_ENERGY_DAILY_FALLBACK: False}),
        ):
            with self.subTest(data=data, options=options):
                entry = SimpleNamespace(data=data, options=options)
                resolved = settings.effective_settings(entry)
                self.assertIs(resolved[CONF_ENERGY_DAILY_FALLBACK], False)
                submitted = settings.settings_schema(resolved)({})
                saved = settings.options_from_input(submitted, existing_options=options)
                self.assertIs(saved[CONF_ENERGY_DAILY_FALLBACK], False)

    def test_saved_input_and_native_selector_all_reject_non_booleans(self):
        schema = settings.settings_schema(settings.new_entry_settings())
        for invalid in INVALID_BOOLEANS:
            with self.subTest(value=invalid):
                with self.assertRaises(vol.Invalid):
                    settings.options_from_input({CONF_ENERGY_DAILY_FALLBACK: invalid})
                with self.assertRaises(vol.Invalid):
                    settings.effective_settings(
                        SimpleNamespace(data={}, options={CONF_ENERGY_DAILY_FALLBACK: invalid})
                    )
                with self.assertRaises(vol.Invalid):
                    schema({CONF_ENERGY_DAILY_FALLBACK: invalid})

    def test_shared_strict_selector_preserves_both_boolean_choices(self):
        schema = settings.settings_schema(settings.new_entry_settings())
        for value in (False, True):
            actual = schema({CONF_ENERGY_DAILY_FALLBACK: value, CONF_SHOULD_IMPORT_HISTORY: value})
            self.assertIs(actual[CONF_ENERGY_DAILY_FALLBACK], value)
            self.assertIs(actual[CONF_SHOULD_IMPORT_HISTORY], value)

    def test_actual_ha_form_serializer_keeps_a_friendly_boolean_toggle(self):
        fields = probatio.to_field_list(
            settings.settings_schema(settings.new_entry_settings()),
            custom_serializer=custom_serializer,
        )
        field = next(item for item in fields if item["name"] == CONF_ENERGY_DAILY_FALLBACK)
        self.assertEqual(field["selector"], {"boolean": {}})
        self.assertIs(field["default"], True)

    def test_settings_labels_explain_aggregate_fallback_and_retained_old_history(self):
        directory = Path(settings.__file__).parent
        paths = [directory / "strings.json", *(directory / "translations").glob("*.json")]
        for path in paths:
            translated = json.loads(path.read_text())
            for area, step in (("config", "import_settings"), ("options", "init")):
                with self.subTest(path=path.name, area=area):
                    form = translated[area]["step"][step]
                    self.assertEqual(
                        form["data"][CONF_ENERGY_DAILY_FALLBACK],
                        "Use daily totals when hours are incomplete",
                    )
                    description = form["data_description"][CONF_ENERGY_DAILY_FALLBACK]
                    self.assertIn("one aggregate bar", description)
                    self.assertIn("daily total", description)
                    self.assertIn("partial recent hours", description)
                    self.assertIn("Older history remains daily", description)


class EnergySettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = entry_fixture.EntrySafetyTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    async def test_initial_setup_persists_false_without_reauthenticating(self):
        flow = self.fixture.flow()
        with patch.object(
            config_flow, "validate_credentials", AsyncMock(return_value=entry_fixture.INFO)
        ) as validate:
            first = await flow.async_step_user(entry_fixture.CREDENTIALS)
            self.assertEqual(first["step_id"], "import_settings")
            result = await flow.async_step_import_settings({CONF_ENERGY_DAILY_FALLBACK: False})
        self.assertEqual(result["type"], "create_entry")
        self.assertIs(result["options"][CONF_ENERGY_DAILY_FALLBACK], False)
        validate.assert_awaited_once()

    async def test_invalid_setup_choice_is_correctable_without_another_login(self):
        flow = self.fixture.flow()
        with patch.object(
            config_flow, "validate_credentials", AsyncMock(return_value=entry_fixture.INFO)
        ) as validate:
            await flow.async_step_user(entry_fixture.CREDENTIALS)
            invalid = await flow.async_step_import_settings({CONF_ENERGY_DAILY_FALLBACK: "false"})
            self.assertEqual(invalid["errors"], {"base": "invalid_settings"})
            result = await flow.async_step_import_settings({CONF_ENERGY_DAILY_FALLBACK: False})
        self.assertEqual(result["type"], "create_entry")
        self.assertIs(result["options"][CONF_ENERGY_DAILY_FALLBACK], False)
        validate.assert_awaited_once()

    async def test_options_preserve_false_other_values_and_authentication_data(self):
        self.fixture.entry.options = {
            CONF_ENERGY_DAILY_FALLBACK: False,
            "future_option": "keep",
        }
        before = deepcopy(self.fixture.entry.data)
        flow = config_flow.AiguesBarcelonaHistoryOptionsFlow()
        flow.hass = self.fixture.hass
        flow.handler = self.fixture.entry.entry_id
        flow.flow_id = "offline-energy-options"
        with patch.object(config_flow, "validate_credentials", new_callable=AsyncMock) as validate:
            form = await flow.async_step_init()
            self.assertIs(
                form["data_schema"]({})[CONF_ENERGY_DAILY_FALLBACK],
                False,
            )
            invalid = await flow.async_step_init({CONF_ENERGY_DAILY_FALLBACK: 1})
            self.assertEqual(invalid["errors"], {"base": "invalid_settings"})
            result = await flow.async_step_init({CONF_ENERGY_DAILY_FALLBACK: False})
            validate.assert_not_awaited()
        self.assertEqual(result["type"], "create_entry")
        self.assertIs(result["data"][CONF_ENERGY_DAILY_FALLBACK], False)
        self.assertEqual(result["data"]["future_option"], "keep")
        self.assertEqual(self.fixture.entry.data, before)
        self.fixture.entries.async_update_entry.assert_not_called()
