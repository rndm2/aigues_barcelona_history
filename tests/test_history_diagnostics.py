"""Diagnostics contain operational metadata without private source data."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.aigues_barcelona_history import diagnostics
from custom_components.aigues_barcelona_history.const import (
    CONF_AUTH_SESSION_LIMIT,
    CONF_RETRY_NOT_BEFORE,
)

PRIVATE = "PRIVATE_DIAGNOSTICS_MARKER"


class HistoryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch("socket.create_connection", side_effect=AssertionError("Network disabled"))
        )
        self.enterContext(
            patch("requests.Session.request", side_effect=AssertionError("Network disabled"))
        )
        self.coordinator = SimpleNamespace(
            id=PRIVATE,
            contract=PRIVATE,
            entry_id=PRIVATE,
            last_update_success=True,
            _stopping=False,
            _should_import_history=False,
            _data={"token": PRIVATE, "meter_reading": 98765.4321},
            _api=SimpleNamespace(login=Mock(side_effect=AssertionError("Login disabled"))),
            _ledger={
                "DAILY": {PRIVATE: {"delta": 98765.4321}},
                "HOURLY": {PRIVATE: {"observed_at": PRIVATE}, "second": {}},
            },
        )
        self.entry = SimpleNamespace(
            entry_id=PRIVATE,
            title=PRIVATE,
            unique_id=PRIVATE,
            state=ConfigEntryState.LOADED,
            data={
                "username": PRIVATE,
                "password": PRIVATE,
                "token": PRIVATE,
                "twocaptcha_api_key": PRIVATE,
                "contract": [PRIVATE],
                CONF_AUTH_SESSION_LIMIT: False,
                CONF_RETRY_NOT_BEFORE: 0,
            },
            options={
                "history_days": 30,
                "hourly_history_days": 7,
                "scan_period": 3900,
                "should_import_history": False,
                "unrelated_private_option": PRIVATE,
            },
            runtime_data=(self.coordinator,),
        )

    async def report(self):
        result = await diagnostics.async_get_config_entry_diagnostics(None, self.entry)
        serialized = json.dumps(result, allow_nan=False)
        self.assertNotIn(PRIVATE, serialized)
        self.assertNotIn("98765.4321", serialized)
        self.coordinator._api.login.assert_not_called()
        return result

    async def test_only_allowlisted_metadata_is_returned_without_mutating_state(self):
        before_data, before_options = deepcopy(self.entry.data), deepcopy(self.entry.options)
        result = await self.report()
        self.assertEqual(
            result,
            {
                "integration_version": diagnostics.VERSION,
                "entry_state": "loaded",
                "configuration_valid": True,
                "settings": {
                    "history_days": 30,
                    "hourly_history_days": 7,
                    "scan_period": 3900,
                    "should_import_history": False,
                },
                "authentication": {"session_limit_blocked": False, "retry_pending": False},
                "meters": [
                    {
                        "available": True,
                        "stopping": False,
                        "startup_import_pending": False,
                        "ledger_loaded": True,
                        "saved_intervals": {"daily": 1, "hourly": 2},
                        "import_status": {
                            "phase": "unknown",
                            "frequency": None,
                            "has_error": False,
                            "completed_windows": None,
                            "total_windows": None,
                        },
                    }
                ],
            },
        )
        self.assertEqual(self.entry.data, before_data)
        self.assertEqual(self.entry.options, before_options)

    async def test_recursive_private_data_is_never_walked_or_serialized(self):
        self.entry.data["self"] = self.entry.data
        self.entry.options["self"] = self.entry.options
        self.coordinator._data["coordinator"] = self.coordinator
        self.coordinator._data["self"] = self.coordinator._data
        self.coordinator._ledger["HOURLY"][PRIVATE]["self"] = self.coordinator._ledger
        result = await self.report()
        self.assertEqual(result["meters"][0]["saved_intervals"], {"daily": 1, "hourly": 2})

    async def test_progress_excludes_private_window_dates_and_all_error_details(self):
        self.coordinator._data["import_status"] = {
            "phase": "retrying",
            "frequency": "HOURLY",
            "completed_windows": 3,
            "total_windows": 54,
            "current_window_start": PRIVATE,
            "current_window_end": PRIVATE,
            "last_error": PRIVATE,
            "provider_response": PRIVATE,
        }
        result = await self.report()
        self.assertEqual(
            result["meters"][0]["import_status"],
            {
                "phase": "retrying",
                "frequency": "HOURLY",
                "has_error": True,
                "completed_windows": 3,
                "total_windows": 54,
            },
        )

    async def test_malformed_progress_cannot_disclose_private_values(self):
        status = {
            "phase": PRIVATE,
            "frequency": PRIVATE,
            "completed_windows": PRIVATE,
            "total_windows": True,
            "last_error": {"private": PRIVATE},
        }
        status["self"] = status
        self.coordinator._data["import_status"] = status
        result = await self.report()
        self.assertEqual(
            result["meters"][0]["import_status"],
            {
                "phase": "unknown",
                "frequency": None,
                "has_error": False,
                "completed_windows": None,
                "total_windows": None,
            },
        )

    async def test_failed_setup_without_runtime_data_remains_diagnosable(self):
        del self.entry.runtime_data
        self.entry.state = ConfigEntryState.SETUP_ERROR
        self.assertEqual((await self.report())["meters"], [])
        self.assertEqual((await self.report())["entry_state"], "setup_error")

    async def test_unloaded_ledger_is_unknown_instead_of_an_invented_zero(self):
        self.coordinator._ledger = None
        result = await self.report()
        self.assertFalse(result["meters"][0]["ledger_loaded"])
        self.assertEqual(result["meters"][0]["saved_intervals"], {"daily": None, "hourly": None})

    async def test_invalid_saved_settings_do_not_disclose_the_bad_value(self):
        self.entry.options["history_days"] = PRIVATE
        result = await self.report()
        self.assertFalse(result["configuration_valid"])
        self.assertIsNone(result["settings"])

    async def test_blocked_retrying_and_stopping_status_does_not_expose_deadline(self):
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        self.entry.data[CONF_RETRY_NOT_BEFORE] = 1234567890
        self.coordinator.last_update_success = False
        self.coordinator._shutdown_requested = True
        self.coordinator._should_import_history = True
        with patch.object(diagnostics, "time", return_value=1234567880):
            result = await self.report()
        self.assertEqual(
            result["authentication"], {"session_limit_blocked": True, "retry_pending": True}
        )
        self.assertNotIn("1234567890", json.dumps(result))
        self.assertFalse(result["meters"][0]["available"])
        self.assertTrue(result["meters"][0]["stopping"])
        self.assertTrue(result["meters"][0]["startup_import_pending"])

    async def test_malformed_retry_status_and_unknown_lifecycle_are_safe(self):
        self.entry.state = PRIVATE
        for value in (True, PRIVATE, float("inf"), float("nan"), 10**1000):
            with self.subTest(value_type=type(value).__name__):
                self.entry.data[CONF_RETRY_NOT_BEFORE] = value
                result = await self.report()
                self.assertEqual(result["entry_state"], "unknown")
                self.assertFalse(result["authentication"]["retry_pending"])
