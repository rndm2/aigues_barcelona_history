"""Exercise real response parsing and token handling without external services."""

from __future__ import annotations

import base64
from datetime import date, datetime, timezone
import json
import math
from types import MethodType
import unittest
from unittest.mock import Mock, patch

from homeassistant.const import CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryAuthFailed
import requests
from test_aigues_barcelona_history import OfflineTests, make_coordinator

from custom_components.aigues_barcelona_history import api as provider
from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.api import (
    AiguesApiAuthError,
    AiguesApiError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
    AiguesApiSessionLimitError,
    AiguesBarcelonaHistoryApiClient,
)
from custom_components.aigues_barcelona_history.const import (
    CONF_2CAPTCHA_APIKEY,
    CONF_AUTH_SESSION_LIMIT,
)
from custom_components.aigues_barcelona_history.statistics import Frequency

PRIVATE = "PRIVATE_RESPONSE_TOKEN_AND_PERSONAL_DETAILS"
# Exact structure and error code observed from the provider's login response.
SESSION_LIMIT_RESPONSE = {
    "errorMessage": "Se ha producido un error inesperado",
    "result": False,
    "errorCode": "MAX_SESSIONS_REACHED_ERROR",
}
REAL_LOGIN = AiguesBarcelonaHistoryApiClient.login
NOW = datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc).timestamp()


def token_for(payload):
    """Build an unsigned fixture token used only for its non-security hints."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def client_for(status=200, payload=None, headers=None):
    """Use an actual requests Response and CookieJar; mock only transport."""
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload if payload is not None else {}).encode()
    response.headers.update(headers or {})
    session = Mock(spec=requests.Session)
    session.cookies = requests.cookies.RequestsCookieJar()
    session.request.return_value = response
    client = AiguesBarcelonaHistoryApiClient(
        "test-user", "test-password", "test-captcha-key", session=session
    )
    return client, session


class TokenSafetyTests(unittest.TestCase):
    def test_expiry_uses_absolute_time_across_the_repeated_autumn_hour(self):
        client, _ = client_for()
        expiry = datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc).timestamp()
        client.set_token(token_for({"exp": expiry, "name": "test-user"}))
        # Madrid's first 02:45 precedes its second 02:15 by thirty minutes.
        with patch.object(provider.time, "time", return_value=NOW):
            self.assertFalse(client.is_token_expired())
        with patch.object(provider.time, "time", return_value=expiry - 0.001):
            self.assertFalse(client.is_token_expired())
        with patch.object(provider.time, "time", return_value=expiry):
            self.assertTrue(client.is_token_expired())
        with patch.object(provider.time, "time", return_value=expiry + 0.001):
            self.assertTrue(client.is_token_expired())
        self.assertEqual(client._return_token_field("name"), "test-user")

    def test_malformed_tokens_are_expired_without_crashing_or_logging_them(self):
        client, _ = client_for()
        malformed = (
            None,
            42,
            "",
            "corrupted",
            "header.@@@.signature",
            "header.eyJleHAi.signature",
            token_for([]),
            token_for("text"),
        )
        with patch.object(provider._LOGGER, "warning") as warning:
            for token in malformed:
                with self.subTest(token_type=type(token).__name__):
                    with patch.object(client, "get_token", return_value=token):
                        self.assertTrue(client.is_token_expired())
                        self.assertIsNone(client._return_token_field("name"))
        warning.assert_not_called()

    def test_invalid_numeric_dates_are_expired_instead_of_triggering_parser_errors(self):
        client, _ = client_for()
        bad_expiry = (None, "1792890900", True, False, math.nan, math.inf, -math.inf, 10**1000, -1)
        with patch.object(provider.time, "time", return_value=NOW):
            for expiry in bad_expiry:
                with self.subTest(expiry_type=type(expiry).__name__):
                    client.set_token(token_for({"exp": expiry}))
                    self.assertTrue(client.is_token_expired())
            client.set_token(token_for({"name": "test-user"}))
            self.assertTrue(client.is_token_expired())

    def test_cookie_scope_and_security_attributes_are_preserved(self):
        client, session = client_for()
        client.set_token(token_for({"exp": NOW + 60}))
        cookie = next(iter(session.cookies))
        self.assertEqual(cookie.domain, ".aiguesdebarcelona.cat")
        self.assertEqual(cookie.path, "/")
        self.assertTrue(cookie.secure)
        self.assertEqual(client.get_token(), cookie.value)


class ResponseSafetyTests(unittest.TestCase):
    def test_error_messages_and_logs_keep_status_without_private_response_payloads(self):
        cases = (
            (400, AiguesApiError),
            (401, AiguesApiAuthError),
            (403, AiguesApiAuthError),
            (408, AiguesApiServerError),
            (429, AiguesApiRateLimitError),
            (500, AiguesApiServerError),
            (503, AiguesApiServerError),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                client, session = client_for(status, {"message": PRIVATE})
                with patch.object(provider._LOGGER, "debug") as debug:
                    with self.assertRaises(expected) as caught:
                        client._query("/meter/consumptions")
                self.assertEqual(caught.exception.status_code, status)
                self.assertIsNone(caught.exception.error_code)
                self.assertNotIn(PRIVATE, str(caught.exception))
                self.assertNotIn(PRIVATE, repr(debug.call_args_list))
                self.assertEqual(client.last_response, {"message": PRIVATE})
                session.request.assert_called_once()

    def test_transport_errors_keep_retry_type_but_remove_full_urls_and_captcha_codes(self):
        for kind in (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ):
            with self.subTest(kind=kind.__name__):
                client, session = client_for()
                session.request.side_effect = kind(
                    f"https://provider/auth?recaptchaClientResponse={PRIVATE}"
                )
                with self.assertRaises(kind) as caught:
                    client._query("/auth/getToken")
                self.assertNotIn(PRIVATE, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                session.request.assert_called_once()

    def test_rate_limit_delay_preserves_provider_seconds_including_long_waits(self):
        for header, expected in (
            ("0", 0.0),
            ("1", 1.0),
            ("120", 120.0),
            ("8640000", 8640000.0),
            ("1.5", 1.5),
        ):
            with self.subTest(header=header):
                client, _ = client_for(429, {}, {"Retry-After": header})
                with self.assertRaises(AiguesApiRateLimitError) as caught:
                    client._query("/meter/consumptions")
                self.assertEqual(caught.exception.retry_after, expected)

    def test_http_date_delay_uses_epoch_time_and_past_dates_mean_retry_now(self):
        cases = (
            ("Sun, 25 Oct 2026 00:47:00 GMT", 120.0),
            ("Sun, 25 Oct 2026 00:44:00 GMT", 0.0),
            ("Sun Oct 25 00:47:00 2026", 120.0),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                client, _ = client_for(503, {}, {"Retry-After": header})
                with patch.object(provider.time, "time", return_value=NOW):
                    with self.assertRaises(AiguesApiServerError) as caught:
                        client._query("/meter/consumptions")
                self.assertEqual(caught.exception.retry_after, expected)

    def test_invalid_retry_headers_do_not_create_negative_or_infinite_delays(self):
        for header in ("", "-1", "NaN", "Infinity", "1e9999", "not a date"):
            with self.subTest(header=header):
                client, _ = client_for(429, {}, {"Retry-After": header})
                with self.assertRaises(AiguesApiRateLimitError) as caught:
                    client._query("/meter/consumptions")
                self.assertIsNone(caught.exception.retry_after)

    def test_nested_session_limits_override_success_or_server_error_status(self):
        payloads = (
            {"errorMessage": "MAX_SESSIONS"},
            {"message": {"error": {"code": "MAX_SESSIONS", "message": PRIVATE}}},
            {"message": json.dumps({"errorMessage": "MAX_SESSIONS", "details": PRIVATE})},
        )
        for status in (200, 500):
            for payload in payloads:
                with self.subTest(status=status, payload_shape=list(payload)):
                    client, session = client_for(status, payload)
                    with self.assertRaises(AiguesApiSessionLimitError) as caught:
                        client._query("/meter/consumptions")
                    self.assertEqual(caught.exception.status_code, status)
                    self.assertEqual(caught.exception.error_code, "MAX_SESSIONS")
                    self.assertIsInstance(caught.exception, AiguesApiAuthError)
                    self.assertNotIsInstance(caught.exception, AiguesApiServerError)
                    self.assertNotIsInstance(caught.exception, AiguesApiRateLimitError)
                    self.assertNotIn(PRIVATE, str(caught.exception))
                    session.request.assert_called_once()

    def test_observed_session_limit_code_is_recognized_directly_and_in_nested_json(self):
        payloads = (SESSION_LIMIT_RESPONSE, {"message": json.dumps(SESSION_LIMIT_RESPONSE)})
        for status in (200, 500):
            for payload in payloads:
                with self.subTest(status=status, nested="message" in payload):
                    client, session = client_for(status, payload)
                    with self.assertRaises(AiguesApiSessionLimitError) as caught:
                        client._query("/auth/getToken")
                    self.assertEqual(caught.exception.error_code, "MAX_SESSIONS_REACHED_ERROR")
                    self.assertEqual(caught.exception.status_code, status)
                    self.assertNotIsInstance(caught.exception, AiguesApiServerError)
                    session.request.assert_called_once()

    def test_known_revoked_token_remains_an_auth_error_without_private_payloads(self):
        for status in (200, 400, 500):
            with self.subTest(status=status):
                client, _ = client_for(status, {"message": f"JWT Token Revoked: {PRIVATE}"})
                with self.assertRaises(AiguesApiAuthError) as caught:
                    client._query("/meter/consumptions")
                self.assertEqual(caught.exception.error_code, "JWT_TOKEN_REVOKED")
                self.assertNotIsInstance(caught.exception, AiguesApiSessionLimitError)
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_unrelated_successful_data_is_not_mistaken_for_a_session_limit(self):
        payload = {"data": [{"description": "MAX_SESSIONS", "deltaConsumption": 0.01}]}
        client, session = client_for(200, payload)
        self.assertIs(client._query("/meter/consumptions"), session.request.return_value)
        self.assertIsNone(provider._provider_error_code({"message": "NOT_MAX_SESSIONS"}))


class PaidLoginSafetyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.object(AiguesBarcelonaHistoryApiClient, "_global_captcha_cooldown_until", None)
        )
        self.enterContext(
            patch.object(AiguesBarcelonaHistoryApiClient, "_global_login_in_progress", False)
        )
        self.solver = Mock()
        self.solver.recaptcha.return_value = {"code": "test-solution"}
        self.enterContext(patch.object(provider, "TwoCaptcha", return_value=self.solver))

    def test_session_limit_is_one_paid_solution_and_one_post_with_no_login_retry(self):
        payloads = ({"errorMessage": "MAX_SESSIONS"}, SESSION_LIMIT_RESPONSE)
        for status in (200, 500):
            for payload in payloads:
                with self.subTest(status=status, code=payload.get("errorCode", "MAX_SESSIONS")):
                    AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until = None
                    self.solver.recaptcha.reset_mock()
                    client, session = client_for(status, payload)
                    with self.assertRaises(AiguesApiSessionLimitError) as caught:
                        client.login()
                    self.assertEqual(
                        caught.exception.error_code, payload.get("errorCode", "MAX_SESSIONS")
                    )
                    self.solver.recaptcha.assert_called_once()
                    session.request.assert_called_once()
                    self.assertEqual(session.request.call_args.kwargs["method"], "POST")
                    self.assertFalse(AiguesBarcelonaHistoryApiClient._global_login_in_progress)

    def test_unknown_rejection_and_missing_token_preserve_false_result_without_leaking_payloads(
        self,
    ):
        for payload in ({"errorMessage": PRIVATE}, {"message": PRIVATE}, {}, []):
            with self.subTest(payload_type=type(payload).__name__):
                AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until = None
                client, session = client_for(200, payload)
                with patch.object(provider._LOGGER, "warning") as warning:
                    self.assertFalse(client.login())
                self.assertNotIn(PRIVATE, repr(warning.call_args_list))
                session.request.assert_called_once()

    def test_solver_failure_or_missing_solution_cannot_expose_private_payloads(self):
        cases = (
            RuntimeError(PRIVATE),
            provider.api.NetworkException(PRIVATE),
            provider.api.ApiException(PRIVATE),
            {"captchaId": PRIVATE},
        )
        for outcome in cases:
            with self.subTest(outcome_type=type(outcome).__name__):
                AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until = None
                self.solver.recaptcha.reset_mock()
                self.solver.recaptcha.side_effect = (
                    outcome if isinstance(outcome, Exception) else None
                )
                self.solver.recaptcha.return_value = outcome if isinstance(outcome, dict) else None
                client, session = client_for()
                with patch.object(provider._LOGGER, "error") as error_log:
                    with self.assertRaises(Exception) as caught:
                        client.login()
                self.assertNotIn(PRIVATE, str(caught.exception))
                self.assertNotIn(PRIVATE, repr(error_log.call_args_list))
                session.request.assert_not_called()
                self.assertFalse(AiguesBarcelonaHistoryApiClient._global_login_in_progress)

    def test_successful_login_preserves_provider_request_protocol(self):
        token = token_for({"exp": NOW + 3600, "name": "test-user"})
        client, session = client_for(200, {"access_token": token})
        self.assertTrue(client.login())
        request = session.request.call_args.kwargs
        self.assertEqual(request["method"], "POST")
        self.assertTrue(request["url"].endswith("/ofex-login-api/auth/getToken"))
        self.assertEqual(
            request["params"], {"lang": "ca", "recaptchaClientResponse": "test-solution"}
        )
        self.assertEqual(
            request["json"],
            {
                "scope": "ofex",
                "companyIdentification": "",
                "userIdentification": "test-user",
                "password": "test-password",
            },
        )
        self.assertEqual(client.get_token(), token)
        self.solver.recaptcha.assert_called_once()


class ObservedSessionLimitCoordinatorTests(OfflineTests):
    """Prove the observed provider code persists a guard, not another paid task."""

    def setUp(self):
        super().setUp()
        self.enterContext(
            patch.object(AiguesBarcelonaHistoryApiClient, "_global_captcha_cooldown_until", None)
        )
        self.enterContext(
            patch.object(AiguesBarcelonaHistoryApiClient, "_global_login_in_progress", False)
        )
        self.entry = self.coordinator.hass.config_entries.async_get_entry.return_value
        self.entry.data = {
            "token": token_for({"exp": 0, "name": "test-user"}),
            CONF_PASSWORD: "test-password",
            CONF_2CAPTCHA_APIKEY: "test-captcha-key",
        }
        self.solver = Mock()
        self.solver.recaptcha.return_value = {"code": "test-solution"}
        self.enterContext(patch.object(provider, "TwoCaptcha", return_value=self.solver))

    def bind_client(self, coordinator, status=500):
        client, session = client_for(status, SESSION_LIMIT_RESPONSE)
        client.set_token(self.entry.data["token"])
        # Override OfflineTests' class-level no-login trap only on this mocked
        # transport client. The real SDK and network remain unreachable.
        client.login = MethodType(REAL_LOGIN, client)
        coordinator._api = client
        coordinator._ensure_token = MethodType(
            sensor.AiguesHistoryCoordinator._ensure_token, coordinator
        )
        coordinator._stopping = False
        return session

    async def test_actual_login_code_persists_guard_and_blocks_a_restarted_coordinator(self):
        session = self.bind_client(self.coordinator)
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.coordinator._ensure_token()
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.solver.recaptcha.assert_called_once()
        session.request.assert_called_once()

        restarted = make_coordinator()
        restarted.hass = self.coordinator.hass
        restarted_session = self.bind_client(restarted)
        # Even clearing the API's in-memory cooldown cannot bypass the saved flag.
        AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until = None
        with self.assertRaises(ConfigEntryAuthFailed):
            await restarted._ensure_token()
        restarted_session.request.assert_not_called()
        self.solver.recaptcha.assert_called_once()

    async def test_actual_consumption_code_also_persists_guard_without_buying_a_solution(self):
        session = self.bind_client(self.coordinator, status=200)
        self.coordinator._api.is_token_expired = Mock(return_value=False)
        with self.assertRaises((AiguesApiSessionLimitError, ConfigEntryAuthFailed)):
            await self.coordinator._request_consumptions(
                date(2026, 10, 27), date(2026, 10, 28), Frequency.HOURLY
            )
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        session.request.assert_called_once()
        self.solver.recaptcha.assert_not_called()


if __name__ == "__main__":
    unittest.main()
