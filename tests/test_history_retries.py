"""Offline regression coverage for bounded, consumption-only retries."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import requests
from test_aigues_barcelona_history import (
    DAILY_ID,
    OfflineTests,
    reading,
)

from custom_components.aigues_barcelona_history import sensor
from custom_components.aigues_barcelona_history.api import (
    AiguesApiAuthError,
    AiguesApiError,
    AiguesApiRateLimitError,
    AiguesApiServerError,
    AiguesBarcelonaHistoryApiClient,
)
from custom_components.aigues_barcelona_history.statistics import Frequency

START = date(2026, 10, 27)
END = date(2026, 10, 29)


class ConsumptionRetryTests(OfflineTests):
    """Retry transient GET failures without inventing source data or partial saves."""

    def setUp(self):
        super().setUp()
        self.sleep = self.enterContext(
            patch.object(sensor.asyncio, "sleep", new_callable=AsyncMock)
        )

    async def test_transient_failures_retry_the_same_request_for_both_frequencies(self):
        failures = (
            requests.exceptions.Timeout("temporary timeout"),
            requests.exceptions.ConnectionError("temporary connection failure"),
            requests.exceptions.ChunkedEncodingError("truncated response"),
            AiguesApiServerError("temporary upstream failure"),
            AiguesApiRateLimitError("rate limited"),
        )
        rows = [reading("2026-10-27T10:01:28+01:00", 655.0, 0.062)]
        for frequency in Frequency:
            for failure in failures:
                with self.subTest(frequency=frequency, failure=type(failure).__name__):
                    self.coordinator._api.consumptions.reset_mock()
                    self.sleep.reset_mock()
                    self.coordinator._api.consumptions.side_effect = [failure, rows]
                    result = await self.coordinator._request_consumptions(START, END, frequency)
                    self.assertEqual(result, rows)
                    expected = call(
                        START, END, self.coordinator.contract, frequency=frequency.value
                    )
                    self.assertEqual(
                        self.coordinator._api.consumptions.call_args_list, [expected, expected]
                    )
                    self.sleep.assert_awaited_once_with(1)
                    self.coordinator._api.login.assert_not_called()

    async def test_exhausted_failure_makes_five_attempts_and_four_backoffs(self):
        failure = AiguesApiServerError("upstream unavailable")
        self.coordinator._api.consumptions.side_effect = failure
        with self.assertRaises(AiguesApiServerError) as caught:
            await self.coordinator._request_consumptions(START, END, Frequency.HOURLY)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 5)
        self.assertEqual(self.sleep.await_args_list, [call(1), call(2), call(4), call(8)])
        self.coordinator._api.login.assert_not_called()

    async def test_success_on_fifth_attempt_is_returned_without_an_extra_sleep(self):
        rows = [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        self.coordinator._api.consumptions.side_effect = [
            requests.exceptions.Timeout("timeout") for _ in range(4)
        ] + [rows]
        result = await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        self.assertEqual(result, rows)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 5)
        self.assertEqual(self.sleep.await_args_list, [call(1), call(2), call(4), call(8)])

    async def test_empty_response_is_success_without_retry_or_login(self):
        for frequency in Frequency:
            with self.subTest(frequency=frequency):
                self.coordinator._api.consumptions.reset_mock()
                self.coordinator._api.consumptions.return_value = []
                result = await self.coordinator._request_consumptions(START, END, frequency)
                self.assertEqual(result, [])
                self.coordinator._api.consumptions.assert_called_once()
        self.sleep.assert_not_awaited()
        self.coordinator._api.login.assert_not_called()

    async def test_auth_bad_requests_and_programming_errors_are_not_retried(self):
        failures = (
            AiguesApiAuthError("expired credentials"),
            AiguesApiError("invalid request"),
            ValueError("invalid source schema"),
            RuntimeError("programming failure"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.coordinator._api.consumptions.reset_mock()
                self.coordinator._api.consumptions.side_effect = failure
                with self.assertRaises(type(failure)) as caught:
                    await self.coordinator._request_consumptions(START, END, Frequency.HOURLY)
                self.assertIs(caught.exception, failure)
                self.coordinator._api.consumptions.assert_called_once()
        self.sleep.assert_not_awaited()
        self.coordinator._api.login.assert_not_called()

    async def test_invalid_rows_are_validated_after_fetch_without_retry(self):
        self.coordinator._api.consumptions.return_value = [
            {"datetime": "2026-10-27T10:01:28+01:00", "accumulatedConsumption": 655.0}
        ]
        with self.assertRaises(ValueError):
            await self.coordinator._fetch(START, END, Frequency.HOURLY)
        self.coordinator._api.consumptions.assert_called_once()
        self.sleep.assert_not_awaited()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_retry_logging_does_not_include_provider_error_payload(self):
        private_payload = "SECRET_TOKEN_AND_PERSONAL_PROVIDER_RESPONSE"
        self.coordinator._api.consumptions.side_effect = [AiguesApiServerError(private_payload), []]
        with patch.object(sensor._LOGGER, "warning") as warning:
            await self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        warning.assert_called()
        self.assertNotIn(private_payload, repr(warning.call_args_list))

    async def test_non_list_response_is_invalid_and_is_not_retried_as_empty(self):
        self.coordinator._api.consumptions.return_value = None
        with self.assertRaises(ValueError):
            await self.coordinator._fetch(START, END, Frequency.HOURLY)
        self.coordinator._api.consumptions.assert_called_once()
        self.sleep.assert_not_awaited()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_cancellation_during_backoff_stops_further_requests(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def backoff(_):
            entered.set()
            await release.wait()

        self.sleep.side_effect = backoff
        self.coordinator._api.consumptions.side_effect = requests.exceptions.Timeout("timeout")
        task = asyncio.create_task(
            self.coordinator._request_consumptions(START, END, Frequency.DAILY)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.coordinator._api.consumptions.assert_called_once()
        self.coordinator._store.async_save.assert_not_awaited()

    async def test_cancellation_during_request_is_not_retried(self):
        self.coordinator._api.consumptions.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.coordinator._request_consumptions(START, END, Frequency.HOURLY)
        self.coordinator._api.consumptions.assert_called_once()
        self.sleep.assert_not_awaited()

    async def test_exhausted_later_frequency_does_not_save_earlier_daily_batch(self):
        daily = [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        self.coordinator._api.consumptions.side_effect = [daily] + [
            AiguesApiServerError("hourly temporarily unavailable") for _ in range(5)
        ]
        with self.assertRaises(AiguesApiServerError):
            await self.coordinator._import_range(START, END, tuple(Frequency))
        self.assertEqual(self.coordinator._api.consumptions.call_count, 6)
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()
        self.assertEqual(self.coordinator._ledger, {"DAILY": {}, "HOURLY": {}})

    async def test_exhausted_later_window_preserves_previously_saved_ledger(self):
        old_rows = [reading("2026-10-27T10:01:28+01:00", 655.0, 0.062)]
        await self.import_frequency(Frequency.HOURLY, old_rows)
        before = deepcopy(self.coordinator._store.saved)
        self.coordinator._store.async_save.reset_mock()
        self.queue.async_import_statistics.reset_mock()
        self.coordinator._api.consumptions.reset_mock()
        self.coordinator._api.consumptions.side_effect = [
            [reading("2026-10-20T10:01:28+01:00", 645.0, 0.02)]
        ] + [AiguesApiServerError("next window unavailable") for _ in range(5)]
        with self.assertRaises(AiguesApiServerError):
            await self.coordinator._import_range(date(2026, 10, 20), END, (Frequency.HOURLY,))
        self.coordinator._store.async_save.assert_not_awaited()
        self.queue.async_import_statistics.assert_not_called()
        self.assertEqual(self.coordinator._store.saved, before)
        self.assertEqual(self.coordinator._ledger, before)
        self.assertEqual(self.coordinator._api.consumptions.call_count, 6)

    async def test_empty_hourly_keeps_previous_hours_and_updates_daily_totals(self):
        hourly = [reading("2026-10-27T10:01:28+01:00", 655.0, 0.062)]
        await self.import_frequency(Frequency.HOURLY, hourly)
        old_hourly = deepcopy(self.coordinator._store.saved["HOURLY"])
        self.coordinator._api.consumptions.reset_mock()
        self.queue.async_import_statistics.reset_mock()
        self.sleep.reset_mock()
        daily = [reading("2026-10-27T23:01:28+01:00", 655.724, 1.478)]
        self.coordinator._api.consumptions.side_effect = lambda *args, **kwargs: (
            daily if kwargs["frequency"] == "DAILY" else []
        )
        await self.coordinator._import_range(START, END, tuple(Frequency))
        self.assertEqual(self.coordinator._store.saved["HOURLY"], old_hourly)
        self.assertEqual(len(self.coordinator._store.saved["DAILY"]), 1)
        self.assertEqual(self.coordinator._data["imported_intervals"], {"daily": 1, "hourly": 1})
        self.assertEqual(set(self.publications()), {DAILY_ID})
        self.sleep.assert_not_awaited()
        hourly_calls = [
            c
            for c in self.coordinator._api.consumptions.call_args_list
            if c.kwargs["frequency"] == "HOURLY"
        ]
        self.assertEqual(len(hourly_calls), 1)
        self.assertEqual(next(iter(old_hourly.values()))["delta"], 0.062)


class ApiStatusClassificationTests(unittest.TestCase):
    """HTTP classification exposes only temporary status failures to retries."""

    def query_with_status(self, status, *, malformed_json=False):
        response = Mock(status_code=status, text="provider response")
        if malformed_json:
            response.json.side_effect = ValueError("non-JSON response")
        else:
            response.json.return_value = {"message": "provider response", "data": []}
        session = Mock()
        session.request.return_value = response
        client = AiguesBarcelonaHistoryApiClient("unused", "unused", "unused", session=session)
        return client, response

    def test_temporary_http_statuses_are_retryable_server_errors(self):
        for status in (408, 500, 502, 503, 504, 599):
            with self.subTest(status=status):
                client, _ = self.query_with_status(status)
                with self.assertRaises(AiguesApiServerError):
                    client._query("/test")
                client.cli.request.assert_called_once()

    def test_rate_limit_status_has_its_own_retryable_exception(self):
        client, _ = self.query_with_status(429)
        with self.assertRaises(AiguesApiRateLimitError):
            client._query("/test")

    def test_auth_statuses_are_not_generic_server_errors(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client, _ = self.query_with_status(status)
                with self.assertRaises(AiguesApiAuthError):
                    client._query("/test")

    def test_other_client_errors_fail_without_becoming_empty_data(self):
        for status in (400, 404, 410, 422):
            with self.subTest(status=status):
                client, _ = self.query_with_status(status)
                with self.assertRaises(AiguesApiError) as caught:
                    client._query("/test")
                self.assertIs(type(caught.exception), AiguesApiError)

    def test_malformed_temporary_error_body_still_has_retryable_classification(self):
        client, _ = self.query_with_status(503, malformed_json=True)
        with self.assertRaises(AiguesApiServerError):
            client._query("/test")

    def test_successful_empty_response_is_returned_unmodified(self):
        client, response = self.query_with_status(200)
        self.assertIs(client._query("/test"), response)
        client.cli.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
