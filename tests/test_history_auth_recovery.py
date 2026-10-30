"""Offline regression coverage for token recovery and upstream hourly gaps."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import MethodType

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_aigues_barcelona_history import (
    DAILY_ID,
    ENERGY_ID,
    HOURLY_ID,
    OfflineTests,
    make_coordinator,
    reading,
)

from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.api import AiguesApiAuthError
from custom_components.aigues_barcelona_history.statistics import Frequency


class AuthRecoveryTests(OfflineTests):
    """Exercise the real coordinator token method with fake API clients."""

    def setUp(self):
        super().setUp()
        self.entry = self.coordinator.hass.config_entries.async_get_entry.return_value
        self.entry.data = {
            "token": "expired-token",
            "password": "unused",
            "twocaptcha_api_key": "unused",
        }
        self.bind_real_token_method(self.coordinator)

    @staticmethod
    def bind_real_token_method(coordinator):
        coordinator._ensure_token = MethodType(
            sensor.AiguesHistoryCoordinator._ensure_token, coordinator
        )
        coordinator._api.current_token = "expired-token"
        coordinator._api.is_token_expired.side_effect = lambda: (
            coordinator._api.current_token != "fresh-token"
        )
        coordinator._api.set_token.side_effect = lambda token: setattr(
            coordinator._api, "current_token", token
        )

    async def test_login_false_is_update_failure_without_forcing_reauth(self):
        self.coordinator._api.login.return_value = False
        with self.assertRaises((RuntimeError, UpdateFailed)) as raised:
            await self.coordinator._ensure_token()
        self.assertNotIsInstance(raised.exception, ConfigEntryAuthFailed)
        self.coordinator._api.login.assert_called_once_with()
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()
        self.assertEqual(self.entry.data["token"], "expired-token")

    async def test_missing_login_token_is_update_failure_without_forcing_reauth(self):
        self.coordinator._api.login.return_value = True
        self.coordinator._api.get_token.return_value = None
        with self.assertRaises((RuntimeError, UpdateFailed)) as raised:
            await self.coordinator._ensure_token()
        self.assertNotIsInstance(raised.exception, ConfigEntryAuthFailed)
        self.coordinator._api.login.assert_called_once_with()
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()
        self.assertEqual(self.entry.data["token"], "expired-token")

    async def test_transient_login_result_surfaces_as_updatefailed_on_refresh(self):
        self.coordinator._api.login.return_value = False
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()
        self.coordinator._api.login.assert_called_once_with()
        self.coordinator._api.consumptions.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_missing_entry_is_auth_failure_and_never_attempts_login(self):
        self.coordinator.hass.config_entries.async_get_entry.return_value = None
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.coordinator._api.login.assert_not_called()
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()

    async def test_explicit_provider_auth_error_still_requests_reauthentication(self):
        self.coordinator._api.login.side_effect = AiguesApiAuthError("Denied")
        with self.assertRaises(ConfigEntryAuthFailed) as raised:
            await self.coordinator._async_update_data()
        self.assertIsInstance(raised.exception.__cause__, AiguesApiAuthError)
        self.coordinator._api.login.assert_called_once_with()
        self.coordinator._api.consumptions.assert_not_called()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_fresh_stored_shared_token_avoids_another_login(self):
        self.entry.data["token"] = "fresh-token"
        await self.coordinator._ensure_token()
        self.coordinator._api.set_token.assert_called_once_with("fresh-token")
        self.coordinator._api.login.assert_not_called()
        self.coordinator._api.get_token.assert_not_called()
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()

    async def test_concurrent_contracts_share_one_login_and_persisted_token(self):
        second = make_coordinator()
        second.hass = self.coordinator.hass
        second._token_lock = self.coordinator._token_lock
        self.bind_real_token_method(second)
        entered, release = asyncio.Event(), asyncio.Event()

        async def first_login():
            entered.set()
            await release.wait()
            self.coordinator._api.current_token = "fresh-token"
            return True

        self.coordinator._api.login.side_effect = first_login
        self.coordinator._api.get_token.return_value = "fresh-token"
        first_task = asyncio.create_task(self.coordinator._ensure_token())
        await asyncio.wait_for(entered.wait(), timeout=2)
        second_task = asyncio.create_task(second._ensure_token())
        await asyncio.sleep(0)
        second._api.login.assert_not_called()
        release.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=2)

        self.coordinator._api.login.assert_called_once_with()
        second._api.login.assert_not_called()
        second._api.set_token.assert_called_once_with("fresh-token")
        self.coordinator.hass.config_entries.async_update_entry.assert_called_once()
        self.assertEqual(self.entry.data["token"], "fresh-token")
        self.assertFalse(second._api.is_token_expired())


class HourlyRecoveryTests(OfflineTests):
    async def test_empty_hourly_preserves_history_then_resumes_real_intervals(self):
        first = reading("2026-10-27T10:01:28+01:00", 100.0, 0.2)
        await self.import_frequency(Frequency.HOURLY, [first])
        previous_hourly = deepcopy(self.coordinator._store.saved["HOURLY"])
        daily = [reading("2026-10-28T17:01:26+01:00", 102.0, 1.7)]

        self.queue.async_import_statistics.reset_mock()
        self.coordinator._api.consumptions.reset_mock()
        self.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else []
        )
        await self.coordinator._async_update_data()

        self.assertEqual(self.coordinator._store.saved["HOURLY"], previous_hourly)
        self.assertEqual(set(self.publications()), {DAILY_ID, ENERGY_ID})
        self.assertEqual(self.publications()[DAILY_ID][1][0]["sum"], 1700)
        hourly_calls = [
            call
            for call in self.coordinator._api.consumptions.call_args_list
            if call.kwargs["frequency"] == "HOURLY"
        ]
        self.assertEqual(
            len(hourly_calls), 2, "Historical and current provider dates are fetched once"
        )
        self.assertEqual(self.coordinator._data["imported_intervals"], {"daily": 1, "hourly": 1})

        recovered = reading("2026-10-28T16:01:26+01:00", 102.0, 0.3)
        self.queue.async_import_statistics.reset_mock()
        self.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else [recovered]
        )
        await self.coordinator._async_update_data()

        self.assertEqual(set(self.publications()), {HOURLY_ID})
        points = self.publications()[HOURLY_ID][1]
        self.assertEqual(len(points), 2, "No hourly records may be synthesized")
        self.assertEqual([point["state"] for point in points], [100000, 102000])
        self.assertEqual([point["sum"] for point in points], [200, 500])
        self.assertEqual((points[1]["start"] - points[0]["start"]).total_seconds(), 30 * 3600)
        self.assertEqual(self.coordinator._data["imported_intervals"], {"daily": 1, "hourly": 2})
