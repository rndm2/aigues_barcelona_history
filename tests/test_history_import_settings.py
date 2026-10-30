"""Initial settings and options remain separate from paid authentication."""

from copy import deepcopy
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
    CONF_HISTORY_DAYS,
    CONF_HOURLY_HISTORY_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SCAN_PERIOD,
    CONF_SHOULD_IMPORT_HISTORY,
)


class SettingsValidationTests(unittest.TestCase):
    def test_existing_settings_keep_separate_ranges_seconds_and_pending_flag(self):
        entry = SimpleNamespace(
            data={CONF_HISTORY_DAYS: 100, CONF_SCAN_PERIOD: 3601},
            options={
                CONF_HISTORY_DAYS: 365,
                CONF_HOURLY_HISTORY_DAYS: 31,
                CONF_SHOULD_IMPORT_HISTORY: True,
            },
        )
        before = deepcopy((entry.data, entry.options))
        self.assertEqual(
            settings.effective_settings(entry),
            {
                CONF_HISTORY_DAYS: 365,
                CONF_HOURLY_HISTORY_DAYS: 31,
                CONF_SCAN_PERIOD: 3601,
                CONF_SHOULD_IMPORT_HISTORY: True,
                CONF_ENERGY_DAILY_FALLBACK: True,
            },
        )
        self.assertEqual((entry.data, entry.options), before)

    def test_legacy_range_applies_to_both_imports_without_enabling_a_new_import(self):
        entry = SimpleNamespace(data={}, options={CONF_HISTORY_DAYS: 90})
        resolved = settings.effective_settings(entry)
        self.assertEqual(resolved[CONF_HISTORY_DAYS], 90)
        self.assertEqual(resolved[CONF_HOURLY_HISTORY_DAYS], 90)
        self.assertFalse(resolved[CONF_SHOULD_IMPORT_HISTORY])
        empty = settings.effective_settings(SimpleNamespace(data={}, options={}))
        self.assertEqual(empty[CONF_HISTORY_DAYS], 1825)
        self.assertEqual(empty[CONF_HOURLY_HISTORY_DAYS], 1825)
        self.assertFalse(empty[CONF_SHOULD_IMPORT_HISTORY])

    def test_options_false_overrides_a_legacy_pending_flag_in_entry_data(self):
        entry = SimpleNamespace(
            data={CONF_SHOULD_IMPORT_HISTORY: True}, options={CONF_SHOULD_IMPORT_HISTORY: False}
        )
        self.assertFalse(settings.effective_settings(entry)[CONF_SHOULD_IMPORT_HISTORY])

    def test_whole_selector_days_are_accepted_and_invalid_days_are_not_rounded(self):
        for value in (1, 31.0, 1825):
            self.assertIsInstance(settings.validate_history_days(value), int)
        for value in (True, False, 0, 1826, -1, 1.5, float("nan"), float("inf"), "7", None):
            with self.subTest(value=value), self.assertRaises(vol.Invalid):
                settings.validate_history_days(value)

    def test_minute_input_persists_seconds_and_preserves_unrelated_options(self):
        existing = {
            CONF_HISTORY_DAYS: 365,
            CONF_HOURLY_HISTORY_DAYS: 90,
            CONF_SCAN_PERIOD: 3900,
            CONF_SHOULD_IMPORT_HISTORY: False,
            "future_option": {"keep": True},
        }
        before = deepcopy(existing)
        result = settings.options_from_input(
            {
                CONF_HOURLY_HISTORY_DAYS: 30.0,
                CONF_HISTORY_DAYS: 1825.0,
                CONF_SCAN_INTERVAL_MINUTES: 65.0,
                CONF_SHOULD_IMPORT_HISTORY: True,
            },
            existing_options=existing,
        )
        self.assertEqual(result[CONF_SCAN_PERIOD], 3900)
        self.assertEqual(result[CONF_HOURLY_HISTORY_DAYS], 30)
        self.assertEqual(result[CONF_HISTORY_DAYS], 1825)
        self.assertEqual(result["future_option"], {"keep": True})
        self.assertNotIn(CONF_SCAN_INTERVAL_MINUTES, result)
        self.assertEqual(existing, before)

    def test_existing_non_minute_refresh_intervals_round_trip_exactly(self):
        for seconds in (900, 3901, 86400):
            with self.subTest(seconds=seconds):
                result = settings.options_from_input(
                    {
                        CONF_SCAN_INTERVAL_MINUTES: seconds / 60,
                    }
                )
                self.assertEqual(result[CONF_SCAN_PERIOD], seconds)
        for minutes in (True, 14, 1441, 65.001, float("nan"), float("inf")):
            with self.subTest(minutes=minutes), self.assertRaises(vol.Invalid):
                settings.options_from_input({CONF_SCAN_INTERVAL_MINUTES: minutes})

    def test_unknown_submitted_keys_and_invalid_boolean_are_rejected(self):
        for request in ({"password": "not an option"}, {CONF_SHOULD_IMPORT_HISTORY: "yes"}):
            with self.subTest(request=request), self.assertRaises(vol.Invalid):
                settings.options_from_input(request)

    def test_schema_uses_days_and_minutes_without_changing_stored_second_units(self):
        schema = settings.settings_schema(settings.new_entry_settings())
        fields = {marker.schema: (marker, validator) for marker, validator in schema.schema.items()}
        self.assertEqual(
            set(fields),
            {
                CONF_HOURLY_HISTORY_DAYS,
                CONF_HISTORY_DAYS,
                CONF_SCAN_INTERVAL_MINUTES,
                CONF_SHOULD_IMPORT_HISTORY,
                CONF_ENERGY_DAILY_FALLBACK,
            },
        )
        self.assertEqual(fields[CONF_SCAN_INTERVAL_MINUTES][0].default(), 65)
        self.assertEqual(
            fields[CONF_SCAN_INTERVAL_MINUTES][1].config["unit_of_measurement"], "minutes"
        )
        self.assertEqual(fields[CONF_HOURLY_HISTORY_DAYS][1].config["max"], 1825)
        self.assertTrue(fields[CONF_SHOULD_IMPORT_HISTORY][0].default())

    def test_actual_ha_serializer_exposes_unit_labelled_numeric_controls(self):
        serialized = probatio.to_field_list(
            settings.settings_schema(settings.new_entry_settings()),
            custom_serializer=custom_serializer,
        )
        encoded = json.loads(json.dumps(serialized))
        fields = {field["name"]: field for field in encoded}
        self.assertEqual(
            fields[CONF_SCAN_INTERVAL_MINUTES]["selector"]["number"]["unit_of_measurement"],
            "minutes",
        )
        self.assertEqual(
            fields[CONF_HOURLY_HISTORY_DAYS]["selector"]["number"]["unit_of_measurement"], "days"
        )


class ImportSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = entry_fixture.EntrySafetyTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    async def authenticated_flow(self):
        flow = self.fixture.flow()
        validate = AsyncMock(return_value=entry_fixture.INFO)
        self.enterContext(patch.object(config_flow, "validate_credentials", validate))
        result = await flow.async_step_user(entry_fixture.CREDENTIALS)
        self.assertEqual((result["type"], result["step_id"]), ("form", "import_settings"))
        return flow, validate

    async def test_initial_setup_asks_both_ranges_then_creates_entry_without_reauth(self):
        flow, validate = await self.authenticated_flow()
        result = await flow.async_step_import_settings(
            {
                CONF_HOURLY_HISTORY_DAYS: 90,
                CONF_HISTORY_DAYS: 730,
                CONF_SCAN_INTERVAL_MINUTES: 65,
                CONF_SHOULD_IMPORT_HISTORY: True,
            }
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["token"], entry_fixture.INFO["token"])
        self.assertEqual(
            result["options"],
            {
                CONF_HOURLY_HISTORY_DAYS: 90,
                CONF_HISTORY_DAYS: 730,
                CONF_SCAN_PERIOD: 3900,
                CONF_SHOULD_IMPORT_HISTORY: True,
                CONF_ENERGY_DAILY_FALLBACK: True,
            },
        )
        validate.assert_awaited_once()

    async def test_invalid_settings_can_be_corrected_without_another_login(self):
        flow, validate = await self.authenticated_flow()
        result = await flow.async_step_import_settings({CONF_HOURLY_HISTORY_DAYS: 0})
        self.assertEqual(result["errors"], {"base": "invalid_settings"})
        result = await flow.async_step_import_settings(
            {
                CONF_HOURLY_HISTORY_DAYS: 31,
                CONF_SHOULD_IMPORT_HISTORY: False,
            }
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertFalse(result["options"][CONF_SHOULD_IMPORT_HISTORY])
        validate.assert_awaited_once()

    async def test_settings_step_cannot_skip_account_validation(self):
        flow = self.fixture.flow()
        result = await flow.async_step_import_settings({CONF_HISTORY_DAYS: 30})
        self.assertEqual((result["type"], result["step_id"]), ("form", "user"))
        self.fixture.api.login.assert_not_called()

    async def test_account_completed_in_another_flow_is_rejected_without_second_login(self):
        flow, validate = await self.authenticated_flow()
        self.fixture.entries.async_entries.return_value = [self.fixture.entry]
        result = await flow.async_step_import_settings({})
        self.assertEqual(result["reason"], "already_configured")
        validate.assert_awaited_once()

    async def test_contract_claimed_while_settings_are_open_is_rejected(self):
        flow, validate = await self.authenticated_flow()
        self.fixture.entries.async_entries.return_value = [
            SimpleNamespace(
                entry_id="other-entry",
                data={"username": "Y1234567L", "contract": ["ONE"]},
            )
        ]
        result = await flow.async_step_import_settings({})
        self.assertEqual(result["reason"], "contract_already_configured")
        validate.assert_awaited_once()

    async def test_options_expose_minutes_preserve_pending_false_and_unrelated_values(self):
        self.fixture.entry.options = {
            CONF_HISTORY_DAYS: 365,
            CONF_SCAN_PERIOD: 3901,
            CONF_SHOULD_IMPORT_HISTORY: False,
            "future_option": "keep",
        }
        flow = config_flow.AiguesBarcelonaHistoryOptionsFlow()
        flow.hass = self.fixture.hass
        flow.handler = self.fixture.entry.entry_id
        flow.flow_id = "offline-options"
        result = await flow.async_step_init()
        fields = {marker.schema: marker for marker in result["data_schema"].schema}
        self.assertEqual(fields[CONF_SCAN_INTERVAL_MINUTES].default(), 3901 / 60)
        self.assertFalse(fields[CONF_SHOULD_IMPORT_HISTORY].default())
        result = await flow.async_step_init(
            {
                CONF_HOURLY_HISTORY_DAYS: 14,
                CONF_SCAN_INTERVAL_MINUTES: 3901 / 60,
            }
        )
        self.assertEqual(result["data"][CONF_SCAN_PERIOD], 3901)
        self.assertEqual(result["data"][CONF_HOURLY_HISTORY_DAYS], 14)
        self.assertEqual(result["data"][CONF_HISTORY_DAYS], 365)
        self.assertEqual(result["data"]["future_option"], "keep")
        self.assertFalse(result["data"][CONF_SHOULD_IMPORT_HISTORY])

    async def test_options_invalid_values_return_form_without_creating_an_entry(self):
        flow = config_flow.AiguesBarcelonaHistoryOptionsFlow()
        flow.hass = self.fixture.hass
        flow.handler = self.fixture.entry.entry_id
        flow.flow_id = "offline-options"
        result = await flow.async_step_init({CONF_SCAN_INTERVAL_MINUTES: 1})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_settings"})
        self.fixture.entries.async_update_entry.assert_not_called()

    async def test_translation_files_describe_both_histories_and_one_time_import(self):
        directory = Path(config_flow.__file__).with_name("translations")
        for path in directory.glob("*.json"):
            with self.subTest(language=path.stem):
                translated = json.loads(path.read_text())
                for section, step in (("config", "import_settings"), ("options", "init")):
                    form = translated[section]["step"][step]
                    self.assertIn(CONF_HOURLY_HISTORY_DAYS, form["data"])
                    self.assertIn(CONF_HISTORY_DAYS, form["data"])
                    self.assertIn("once", form["data"][CONF_SHOULD_IMPORT_HISTORY])
                    self.assertIn("reference", form["data"][CONF_HISTORY_DAYS])
