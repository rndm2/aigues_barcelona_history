"""Reuse configured sessions only after provider validation, without extra logins."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import json
from types import MethodType
from unittest.mock import Mock, patch

from homeassistant.const import CONF_TOKEN, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryAuthFailed
import requests
from test_aigues_barcelona_history import CONTRACT, OfflineTests
from test_history_api_safety import REAL_LOGIN, SESSION_LIMIT_RESPONSE, token_for

from custom_components.aigues_barcelona_history import api as provider
from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.const import (
    CONF_AUTH_SESSION_LIMIT,
    CONF_CONTRACT,
    CONF_RETRY_NOT_BEFORE,
    DOMAIN,
)
from custom_components.aigues_barcelona_history.statistics import Frequency

USER = "X1234567L"
CLIENT = provider.AiguesBarcelonaHistoryApiClient


class ConfiguredSource:
    def __init__(self, token, *, domain="aigues_barcelona", username=USER, entry_id="original"):
        self.domain, self.entry_id = domain, entry_id
        self.data = {
            CONF_USERNAME: username,
            CONF_TOKEN: token,
            "password": "PRIVATE_SOURCE_PASSWORD",
            "twocaptcha_api_key": "PRIVATE_SOURCE_KEY",
        }
        self.options = {"untouched": True}

    @property
    def runtime_data(self):
        raise AssertionError("Another integration's runtime must never be inspected")


class ExistingSessionReuseTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        self.real_sleep = asyncio.sleep
        self.enterContext(patch.object(provider.time, "time", side_effect=lambda: self.clock))
        self.enterContext(patch.object(sensor, "monotonic", side_effect=lambda: self.clock))
        self.enterContext(patch.object(sensor, "MIN_REQUEST_INTERVAL_SECONDS", 1))
        self.enterContext(patch.object(CLIENT, "_global_captcha_cooldown_until", None))
        self.enterContext(patch.object(CLIENT, "_global_login_in_progress", False))

        async def advance(seconds):
            self.clock += seconds
            await self.real_sleep(0)

        self.sleep = self.enterContext(patch.object(sensor.asyncio, "sleep", side_effect=advance))
        self.entry = self.coordinator._entry()
        self.expired = self.jwt(expiry=999)
        self.fresh = self.jwt(expiry=1600)
        self.entry.data = {
            CONF_USERNAME: USER,
            CONF_TOKEN: self.expired,
            "password": "own-password",
            "twocaptcha_api_key": "own-key",
            CONF_CONTRACT: [CONTRACT, "SECOND"],
            CONF_AUTH_SESSION_LIMIT: False,
            CONF_RETRY_NOT_BEFORE: 0,
        }
        self.source = ConfiguredSource(self.fresh)
        self.sources = [self.source]
        self.coordinator.hass.config_entries.async_entries.side_effect = lambda domain: [
            source for source in self.sources if source.domain == domain
        ]
        self.own_transport = self.transport()
        self.coordinator._api = CLIENT(
            USER, "own-password", "own-key", CONTRACT, self.own_transport
        )
        self.coordinator._api.set_token(self.expired)
        self.coordinator._api.login = Mock(
            side_effect=AssertionError("Paid login was not expected")
        )
        self.coordinator._ensure_token = MethodType(
            sensor.AiguesHistoryCoordinator._ensure_token, self.coordinator
        )
        self.validations, self.validation_clients, self.outcomes = [], [], []
        self.rows = [
            {"contractDetail": {"contractNumber": value}} for value in [CONTRACT, "SECOND"]
        ]

        def make_candidate(username, password, key, contract):
            self.assertEqual(username, USER)
            self.assertEqual((password, key), ("", ""))
            transport = self.transport()
            candidate = CLIENT(username, password, key, contract, transport)

            def response(**kwargs):
                self.validations.append((candidate.get_token(), self.clock, kwargs))
                if self.outcomes:
                    outcome = self.outcomes.pop(0)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                return self.response(200, {"data": self.rows})

            transport.request.side_effect = response
            self.validation_clients.append(candidate)
            return candidate

        self.enterContext(
            patch.object(sensor, "AiguesBarcelonaHistoryApiClient", side_effect=make_candidate)
        )

    @staticmethod
    def transport():
        transport = Mock(spec=requests.Session)
        transport.cookies = requests.cookies.RequestsCookieJar()
        transport.request.return_value = ExistingSessionReuseTests.response(200, {"data": []})
        return transport

    @staticmethod
    def response(status, payload):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        return response

    def jwt(self, *, account=USER, expiry=1600):
        return token_for({"name": account, "exp": expiry})

    def assert_no_paid_login(self):
        self.coordinator._api.login.assert_not_called()
        self.assertTrue(all(client.cli.close.call_count == 1 for client in self.validation_clients))

    async def test_fresh_configured_session_is_validated_once_and_source_is_untouched(self):
        before = deepcopy((self.source.data, self.source.options))
        await self.coordinator._ensure_token()
        self.assertEqual(self.entry.data[CONF_TOKEN], self.fresh)
        self.assertEqual(self.coordinator._api.get_token(), self.fresh)
        self.assertEqual(len(self.validations), 1)
        self.assertEqual(self.validations[0][2]["method"], "GET")
        self.assertTrue(self.validations[0][2]["url"].endswith("/ofex-contracts-api/contracts"))
        await self.coordinator._ensure_token()
        self.assertEqual(len(self.validations), 1, "Own valid-token fast path must not revalidate")
        self.assertEqual((self.source.data, self.source.options), before)
        self.assert_no_paid_login()
        self.assertEqual(
            [
                call.args
                for call in self.coordinator.hass.config_entries.async_entries.call_args_list
            ],
            [
                ("aigues_barcelona",),
                (DOMAIN,),
            ],
        )

    async def test_own_valid_session_fast_path_does_not_inspect_other_entries(self):
        self.coordinator._api.set_token(self.fresh)
        self.entry.data[CONF_TOKEN] = self.fresh
        await self.coordinator._ensure_token()
        self.coordinator.hass.config_entries.async_entries.assert_not_called()
        self.assertFalse(self.validation_clients)
        self.assert_no_paid_login()

    async def test_account_and_claim_case_normalization_matches_normal_reauthentication(self):
        token = self.jwt(account=" x1234567l ")
        self.source.data[CONF_USERNAME] = " x1234567l "
        self.source.data[CONF_TOKEN] = token
        await self.coordinator._ensure_token()
        self.assertEqual(self.entry.data[CONF_TOKEN], token)
        self.assertEqual(self.validations[0][2]["params"]["userId"], USER)
        self.assert_no_paid_login()

    async def test_wrong_accounts_and_expired_or_malformed_jwts_never_reach_provider(self):
        cases = [
            ("OTHER", self.fresh),
            (USER, self.jwt(account="OTHER")),
            (USER, self.expired),
            (USER, "not-a-jwt"),
            (USER, token_for({"exp": 1600})),
        ]
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        for account, token in cases:
            with self.subTest(account=account, token_kind=token.split(".")[0]):
                self.source.data.update({CONF_USERNAME: account, CONF_TOKEN: token})
                with self.assertRaises(ConfigEntryAuthFailed):
                    await self.coordinator._ensure_token()
                self.assertEqual(self.entry.data[CONF_TOKEN], self.expired)
                self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertFalse(self.validations)
        self.assert_no_paid_login()

    async def test_revoked_candidate_is_ignored_and_another_valid_session_can_recover(self):
        second = self.jwt(expiry=1700)
        self.sources.append(ConfiguredSource(second, domain=DOMAIN, entry_id="other-history"))
        self.outcomes.append(self.response(401, {"error": "revoked"}))
        await self.coordinator._ensure_token()
        self.assertEqual(self.entry.data[CONF_TOKEN], second)
        self.assertEqual(len(self.validations), 2)
        self.assertGreaterEqual(self.validations[1][1] - self.validations[0][1], 1)
        self.assert_no_paid_login()

    async def test_missing_any_configured_contract_cannot_adopt_or_clear_block(self):
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        self.rows = self.rows[:1]
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.assertEqual(self.entry.data[CONF_TOKEN], self.expired)
        self.assertEqual(self.coordinator._api.get_token(), self.expired)
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assert_no_paid_login()

    async def test_blocked_account_can_reuse_a_valid_session_without_erasing_retry_pause(self):
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        self.entry.data[CONF_RETRY_NOT_BEFORE] = self.clock + 3
        before_pause = self.entry.data[CONF_RETRY_NOT_BEFORE]
        await self.coordinator._ensure_token()
        self.assertFalse(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(self.entry.data[CONF_RETRY_NOT_BEFORE], before_pause)
        self.sleep.assert_awaited_once_with(3)
        self.assert_no_paid_login()

    async def test_expired_source_while_blocked_cannot_trigger_a_paid_login(self):
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        self.source.data[CONF_TOKEN] = self.expired
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.assertFalse(self.validations)
        self.assert_no_paid_login()

    async def test_transient_validation_failures_never_fall_back_to_paid_login(self):
        for error in (
            provider.AiguesApiServerError("Temporary"),
            requests.Timeout("Temporary"),
            provider.AiguesApiRateLimitError("Temporary", retry_after=12),
        ):
            with self.subTest(error=type(error).__name__):
                self.entry.data[CONF_RETRY_NOT_BEFORE] = 0
                self.outcomes.append(error)
                with self.assertRaises(type(error)):
                    await self.coordinator._ensure_token()
                self.assertEqual(self.entry.data[CONF_TOKEN], self.expired)
        self.assertGreater(self.entry.data[CONF_RETRY_NOT_BEFORE], self.clock)
        self.assert_no_paid_login()

    async def test_session_limit_during_validation_blocks_paid_fallback(self):
        self.outcomes.append(provider.AiguesApiSessionLimitError("Limit"))
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assert_no_paid_login()

    async def test_history_still_logs_in_normally_without_an_original_integration(self):
        self.sources.clear()

        def login():
            self.coordinator._api.set_token(self.fresh)
            return True

        self.coordinator._api.login.side_effect = login
        await self.coordinator._ensure_token()
        self.coordinator._api.login.assert_called_once()
        self.assertEqual(self.entry.data[CONF_TOKEN], self.fresh)
        self.assertFalse(self.validations)

    async def test_post_login_max_race_reuses_new_source_session_without_error_log(self):
        self.source.data[CONF_TOKEN] = self.expired
        self.coordinator._api.login = MethodType(REAL_LOGIN, self.coordinator._api)
        solver = Mock()
        solver.recaptcha.return_value = {"code": "synthetic-solution"}
        self.enterContext(patch.object(provider, "TwoCaptcha", return_value=solver))

        def reject_login(**kwargs):
            self.source.data[CONF_TOKEN] = self.fresh
            return self.response(500, SESSION_LIMIT_RESPONSE)

        self.own_transport.request.side_effect = reject_login
        with self.assertNoLogs("custom_components.aigues_barcelona_history", level="ERROR"):
            await self.coordinator._ensure_token()
        solver.recaptcha.assert_called_once()
        self.own_transport.request.assert_called_once()
        self.assertFalse(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(self.entry.data[CONF_TOKEN], self.fresh)
        self.assertEqual(len(self.validations), 1)

    async def test_cancellation_during_post_max_recheck_preserves_the_paid_login_block(self):
        self.source.data[CONF_TOKEN] = self.expired

        def reject_login():
            self.source.data[CONF_TOKEN] = self.fresh
            raise provider.AiguesApiSessionLimitError("Limit")

        self.coordinator._api.login.side_effect = reject_login
        entered = asyncio.Event()
        original_executor = self.coordinator.hass.async_add_executor_job

        async def pending_validation(function, *args):
            if getattr(function, "__name__", "") == "contracts":
                entered.set()
                await asyncio.Event().wait()
            return await original_executor(function, *args)

        self.coordinator.hass.async_add_executor_job = pending_validation
        task = asyncio.create_task(self.coordinator._ensure_token())
        await entered.wait()
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(self.entry.data[CONF_TOKEN], self.expired)
        self.coordinator._api.login.assert_called_once()
        self.assertFalse(self.coordinator._request_gate["lock"].locked())

    async def test_expiry_during_gate_wait_renews_outside_gate_before_one_consumption_get(self):
        near = self.jwt(expiry=self.clock + 0.5)
        self.coordinator._api.set_token(near)
        self.entry.data[CONF_TOKEN] = near
        self.coordinator._request_gate["next_request_at"] = self.clock + 1
        rows = await asyncio.wait_for(
            self.coordinator._request_consumptions(
                date(2026, 10, 27), date(2026, 10, 28), Frequency.HOURLY
            ),
            timeout=2,
        )
        self.assertEqual(rows, [])
        self.assertEqual(len(self.validations), 1)
        self.own_transport.request.assert_called_once()
        self.assertEqual(self.entry.data[CONF_TOKEN], self.fresh)
        self.assert_no_paid_login()

    async def test_candidate_expiring_in_gate_wait_is_not_sent_or_adopted(self):
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        self.source.data[CONF_TOKEN] = self.jwt(expiry=self.clock + 0.5)
        self.coordinator._request_gate["next_request_at"] = self.clock + 1
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.assertFalse(self.validations)
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.assert_no_paid_login()

    async def test_expired_paid_login_response_cannot_start_a_second_paid_attempt(self):
        self.sources.clear()
        self.coordinator._api.login.side_effect = None
        self.coordinator._api.login.return_value = True
        with self.assertRaisesRegex(RuntimeError, "expired or invalid"):
            await self.coordinator._request_consumptions(
                date(2026, 10, 27), date(2026, 10, 28), Frequency.HOURLY
            )
        self.coordinator._api.login.assert_called_once()
        self.own_transport.request.assert_not_called()
        self.assertEqual(self.entry.data[CONF_TOKEN], self.expired)

    async def test_paid_session_expiring_while_waiting_cannot_buy_another_login(self):
        self.sources.clear()

        def short_login():
            self.coordinator._api.set_token(self.jwt(expiry=self.clock + 0.5))
            return True

        self.coordinator._api.login.side_effect = short_login
        self.coordinator._request_gate["next_request_at"] = self.clock + 1
        with self.assertRaisesRegex(RuntimeError, "expired while waiting"):
            await self.coordinator._request_consumptions(
                date(2026, 10, 27), date(2026, 10, 28), Frequency.HOURLY
            )
        self.coordinator._api.login.assert_called_once()
        self.own_transport.request.assert_not_called()
