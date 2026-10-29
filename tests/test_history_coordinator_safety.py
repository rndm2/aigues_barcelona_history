"""Persisted request policy and import lifetime regressions without real IO."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
from types import MethodType
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_aigues_barcelona_history import OfflineTests, make_coordinator, reading
from test_history_api_safety import client_for

from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.api import (
    AiguesApiError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
    AiguesApiSessionLimitError,
)
from custom_components.aigues_barcelona_history.const import (
    CONF_AUTH_SESSION_LIMIT,
    CONF_RETRY_NOT_BEFORE,
)
from custom_components.aigues_barcelona_history.statistics import Frequency

START = date(2026, 10, 27)
END = date(2026, 10, 29)


class CoordinatorSafetyTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.entry = self.coordinator.hass.config_entries.async_get_entry.return_value

    def real_auth(self, coordinator):
        coordinator._ensure_token = MethodType(
            sensor.AiguesHistoryCoordinator._ensure_token, coordinator
        )
        coordinator._api.is_token_expired.return_value = True

    async def test_known_session_limit_blocks_later_paid_attempts_and_restart(self):
        self.real_auth(self.coordinator)
        self.coordinator._api.login.side_effect = AiguesApiSessionLimitError(
            "Session limit", error_code="MAX_SESSIONS_REACHED_ERROR"
        )
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.coordinator._api.login.assert_called_once()
        restarted = make_coordinator()
        restarted.hass = self.coordinator.hass
        self.real_auth(restarted)
        with self.assertRaises(ConfigEntryAuthFailed):
            await restarted._ensure_token()
        restarted._api.login.assert_not_called()
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.coordinator._api.login.assert_called_once()

    async def test_session_limit_on_consumption_get_also_blocks_future_paid_login(self):
        self.coordinator._api.consumptions.side_effect = AiguesApiSessionLimitError(
            "Session limit", error_code="MAX_SESSIONS_REACHED_ERROR"
        )
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.coordinator._api.consumptions.assert_called_once()
        self.real_auth(self.coordinator)
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.coordinator._api.login.assert_not_called()

    async def test_expired_token_only_entry_never_attempts_paid_login(self):
        self.real_auth(self.coordinator)
        self.entry.data = {"token": "expired"}
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.coordinator._api.login.assert_not_called()

    async def test_long_retry_after_is_persisted_and_respected_after_restart(self):
        self.coordinator._api.consumptions.side_effect = AiguesApiRateLimitError(
            "Pause", retry_after=120
        )
        with (
            patch.object(sensor.time, "time", return_value=1000),
            patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            with self.assertRaises(AiguesApiRateLimitError):
                await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
            self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], 1120)
            self.coordinator._api.consumptions.assert_called_once()
            sleep.assert_not_awaited()
            restarted = make_coordinator()
            restarted.hass = self.coordinator.hass
            with self.assertRaises(AiguesApiRateLimitError):
                await restarted._request_consumptions(START, END, Frequency.HOURLY)
            restarted._api.consumptions.assert_not_called()
        self.coordinator._api.consumptions.side_effect = None
        self.coordinator._api.consumptions.return_value = []
        with patch.object(sensor.time, "time", return_value=1121):
            self.assertEqual(
                await self.coordinator._request_consumptions(START, END, Frequency.HOURLY), []
            )
        self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], 0)
        self.coordinator._api.login.assert_not_called()

    async def test_short_retry_after_is_not_shortened_by_exponential_backoff(self):
        self.coordinator._api.consumptions.side_effect = [
            AiguesApiServerError("Pause", retry_after=12),
            [],
        ]
        clock = [1000]

        async def advance(seconds):
            clock[0] += seconds

        with (
            patch.object(sensor.time, "time", side_effect=lambda: clock[0]),
            patch.object(
                sensor.asyncio, "sleep", new_callable=AsyncMock, side_effect=advance
            ) as sleep,
        ):
            self.assertEqual(
                await self.coordinator._request_consumptions(START, END, Frequency.DAILY), []
            )
        sleep.assert_awaited_once_with(12)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 2)
        self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], 0)

    async def test_late_success_cannot_clear_another_contracts_new_pause(self):
        async def response(*args, **kwargs):
            self.entry.data[CONF_RETRY_NOT_BEFORE] = 1120
            return []

        self.coordinator._api.consumptions.side_effect = response
        with patch.object(sensor.time, "time", return_value=1000):
            self.assertEqual(
                await self.coordinator._request_consumptions(START, END, Frequency.DAILY), []
            )
        self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], 1120)

    async def test_a_pause_extended_during_sleep_is_checked_again_before_get(self):
        self.entry.data[CONF_RETRY_NOT_BEFORE] = 1005
        clock = [1000]

        async def extend_while_waiting(seconds):
            clock[0] += seconds
            self.entry.data[CONF_RETRY_NOT_BEFORE] = 1120

        with (
            patch.object(sensor.time, "time", side_effect=lambda: clock[0]),
            patch.object(
                sensor.asyncio,
                "sleep",
                new_callable=AsyncMock,
                side_effect=extend_while_waiting,
            ) as sleep,
        ):
            with self.assertRaises(AiguesApiRateLimitError):
                await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        sleep.assert_awaited_once_with(5)
        self.coordinator._api.consumptions.assert_not_called()

    async def test_long_pause_is_checked_before_any_paid_token_refresh(self):
        self.entry.data[CONF_RETRY_NOT_BEFORE] = 1120
        with patch.object(sensor.time, "time", return_value=1000):
            with self.assertRaises(AiguesApiRateLimitError):
                await self.coordinator._import_range(START, END, tuple(Frequency))
        self.coordinator._ensure_token.assert_not_awaited()
        self.coordinator._api.consumptions.assert_not_called()

    async def test_late_failure_after_shutdown_cannot_persist_a_new_pause(self):
        before = deepcopy(self.entry.data)

        async def fail_after_shutdown(*args, **kwargs):
            self.coordinator._stopping = True
            raise AiguesApiRateLimitError("Pause", retry_after=120)

        self.coordinator._api.consumptions.side_effect = fail_after_shutdown
        with self.assertRaises(asyncio.CancelledError):
            await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.assertEqual(self.entry.data, before)
        self.coordinator.hass.config_entries.async_update_entry.assert_not_called()

    async def test_final_failed_attempt_retains_provider_pause(self):
        self.coordinator._api.consumptions.side_effect = [
            AiguesApiServerError("Temporary") for _ in range(4)
        ] + [AiguesApiRateLimitError("Pause", retry_after=120)]
        with (
            patch.object(sensor.time, "time", return_value=1000),
            patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock),
        ):
            with self.assertRaises(AiguesApiRateLimitError):
                await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 5)
        self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], 1120)

    async def test_invalid_persisted_deadline_fails_before_any_request(self):
        for invalid in (None, "tomorrow", float("nan"), float("inf"), True):
            with self.subTest(kind=type(invalid).__name__):
                self.entry.data[CONF_RETRY_NOT_BEFORE] = invalid
                with self.assertRaises(ValueError):
                    await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.coordinator._api.consumptions.assert_not_called()

    async def test_unload_during_fetch_prevents_later_persistence_and_publication(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def response(*args, **kwargs):
            entered.set()
            await release.wait()
            return [reading("2026-10-28T17:01:26+01:00", 656.48, 0.756)]

        self.coordinator._api.consumptions.side_effect = response
        task = asyncio.create_task(self.coordinator._import_range(START, END, (Frequency.DAILY,)))
        await entered.wait()
        self.coordinator._stopping = True
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()

    async def test_permanent_protocol_errors_are_not_retried(self):
        for status in (501, 505):
            with self.subTest(status=status):
                client, transport = client_for(status)
                self.coordinator._api = client
                with patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaises(AiguesApiError) as caught:
                        await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
                self.assertIs(type(caught.exception), AiguesApiError)
                transport.request.assert_called_once()
                sleep.assert_not_awaited()

    async def test_final_error_does_not_expose_private_exception_values(self):
        private = "private-token-and-provider-payload"
        self.coordinator._api.consumptions.side_effect = ValueError(private)
        with self.assertRaises(UpdateFailed) as caught:
            await self.coordinator._async_update_data()
        self.assertNotIn(private, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_malformed_contract_ids_cannot_become_entities_or_statistic_ids(self):
        for contracts in ("TEST123", [None], [123], [False], [""], ["   "]):
            with self.subTest(kind=type(contracts).__name__), self.assertRaises(ValueError):
                sensor.canonical_contracts(contracts)
        self.assertEqual(sensor.canonical_contracts([" test123 ", "TEST123"]), ["TEST123"])

    async def test_invalid_manual_ranges_do_not_touch_store_or_recorder(self):
        for days in (True, 1.5, 0, 1826):
            with self.subTest(days=days), self.assertRaises(ValueError):
                await self.coordinator.import_old_consumptions(days=days)
        self.coordinator._store.async_load.assert_not_awaited()
        self.coordinator._api.consumptions.assert_not_called()
        self.queue.async_import_statistics.assert_not_called()
