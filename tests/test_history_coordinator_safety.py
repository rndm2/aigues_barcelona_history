"""Persisted request policy and import lifetime regressions without real IO."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
from types import MethodType
from unittest.mock import AsyncMock, Mock, patch

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
    CONF_AUTH_SESSION_LIMIT_ATTEMPTS,
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

    async def test_session_limit_survives_restart_then_retries_with_saved_credentials(self):
        self.real_auth(self.coordinator)
        before = deepcopy(self.entry.data)
        self.coordinator._api.login.side_effect = AiguesApiSessionLimitError(
            "Session limit", error_code="MAX_SESSIONS_REACHED_ERROR"
        )
        with patch.object(sensor.time, "time", return_value=1000):
            with self.assertRaises(AiguesApiSessionLimitError):
                await self.coordinator._ensure_token()
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(self.entry.data[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 1)
        self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], 2800)
        self.coordinator._api.login.assert_called_once()
        restarted = make_coordinator()
        restarted.hass = self.coordinator.hass
        self.real_auth(restarted)
        with patch.object(sensor.time, "time", return_value=1500):
            with self.assertRaises(AiguesApiRateLimitError):
                await restarted._ensure_token()
        restarted._api.login.assert_not_called()
        self.assertEqual({key: self.entry.data[key] for key in before}, before)

        def recover():
            restarted._api.is_token_expired.return_value = False
            return True

        restarted._api.login.side_effect = recover
        restarted._api.get_token.return_value = "renewed-token"
        with patch.object(sensor.time, "time", return_value=2800):
            await restarted._ensure_token()
        restarted._api.login.assert_called_once()
        self.assertFalse(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(self.entry.data[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 0)
        self.assertEqual(self.entry.data["token"], "renewed-token")
        self.assertEqual({key: self.entry.data[key] for key in before}, before)
        self.coordinator._api.login.assert_called_once()

    async def test_session_limit_on_consumption_get_pauses_future_paid_login(self):
        self.coordinator._api.consumptions.side_effect = AiguesApiSessionLimitError(
            "Session limit", error_code="MAX_SESSIONS_REACHED_ERROR"
        )
        with patch.object(sensor.time, "time", return_value=1000):
            with self.assertRaises(AiguesApiSessionLimitError):
                await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.coordinator._api.consumptions.assert_called_once()
        self.real_auth(self.coordinator)
        with patch.object(sensor.time, "time", return_value=1001):
            with self.assertRaises(AiguesApiRateLimitError):
                await self.coordinator._ensure_token()
        self.coordinator._api.login.assert_not_called()

    async def test_legacy_permanent_flag_recovers_without_replacing_credentials_or_ledger(self):
        await self.import_frequency(
            Frequency.HOURLY, [reading("2026-10-27T10:01:24+01:00", 655.0, 0.023)]
        )
        saved = deepcopy(self.coordinator._store.saved)
        ids = deepcopy(self.coordinator.statistic_ids)
        credentials = deepcopy(self.entry.data)
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        self.real_auth(self.coordinator)

        def recover():
            self.coordinator._api.is_token_expired.return_value = False
            return True

        self.coordinator._api.login.side_effect = recover
        self.coordinator._api.get_token.return_value = "renewed-token"
        await self.coordinator._ensure_token()
        self.coordinator._api.login.assert_called_once()
        self.assertFalse(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(self.coordinator._store.saved, saved)
        self.assertEqual(self.coordinator.statistic_ids, ids)
        self.assertEqual({key: self.entry.data[key] for key in credentials}, credentials)

    async def test_concurrent_contracts_cannot_buy_another_login_during_session_pause(self):
        second = make_coordinator()
        second.hass = self.coordinator.hass
        second._token_lock = self.coordinator._token_lock
        for coordinator in (self.coordinator, second):
            self.real_auth(coordinator)
            coordinator._api.login.side_effect = AiguesApiSessionLimitError("Limit")
        entered, release = asyncio.Event(), asyncio.Event()

        async def fail_after_waiter_arrives():
            entered.set()
            await release.wait()
            raise AiguesApiSessionLimitError("Limit")

        self.coordinator._api.login.side_effect = fail_after_waiter_arrives
        with patch.object(sensor.time, "time", return_value=1000):
            first_task = asyncio.create_task(self.coordinator._ensure_token())
            await asyncio.wait_for(entered.wait(), timeout=2)
            second_task = asyncio.create_task(second._ensure_token())
            await asyncio.sleep(0)
            second._api.login.assert_not_called()
            release.set()
            outcomes = await asyncio.wait_for(
                asyncio.gather(first_task, second_task, return_exceptions=True), timeout=2
            )
        self.assertIsInstance(outcomes[0], AiguesApiSessionLimitError)
        self.assertIsInstance(outcomes[1], AiguesApiRateLimitError)
        self.coordinator._api.login.assert_called_once()
        second._api.login.assert_not_called()
        self.assertEqual(self.entry.data[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 1)

    async def test_late_success_preserves_newer_session_pause_and_attempt_count(self):
        async def response(*args, **kwargs):
            self.entry.data.update(
                {
                    CONF_AUTH_SESSION_LIMIT: True,
                    CONF_AUTH_SESSION_LIMIT_ATTEMPTS: 3,
                    CONF_RETRY_NOT_BEFORE: 8200,
                }
            )
            return []

        self.coordinator._api.consumptions.side_effect = response
        with patch.object(sensor.time, "time", return_value=1000):
            await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(self.entry.data[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 3)
        self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], 8200)

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
                client.is_token_expired = Mock(return_value=False)
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
