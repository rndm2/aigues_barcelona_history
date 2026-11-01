"""Exercise the shared durable session-limit policy independently of I/O."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from custom_components.aigues_barcelona_history import auth_retry
from custom_components.aigues_barcelona_history.api import AiguesApiSessionLimitError
from custom_components.aigues_barcelona_history.const import (
    CONF_AUTH_SESSION_LIMIT,
    CONF_AUTH_SESSION_LIMIT_ATTEMPTS,
    CONF_RETRY_NOT_BEFORE,
)


class SessionRetryPolicyTests(unittest.TestCase):
    def test_backoff_increases_caps_and_preserves_all_existing_data(self):
        data = {"username": "synthetic", "password": "secret", "token": "saved"}
        credentials = deepcopy(data)
        now = 1000
        for expected in (1800, 3600, 7200, 14400, 21600, 21600, 21600):
            before = deepcopy(data)
            updates = auth_retry.session_retry_updates(
                data, AiguesApiSessionLimitError("Limit"), now=now
            )
            self.assertEqual(data, before)
            data.update(updates)
            self.assertTrue(data[CONF_AUTH_SESSION_LIMIT])
            self.assertEqual(data[CONF_RETRY_NOT_BEFORE], now + expected)
            self.assertEqual({key: data[key] for key in credentials}, credentials)
            now = data[CONF_RETRY_NOT_BEFORE]
        self.assertEqual(data[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 5)

    def test_provider_retry_after_can_exceed_the_local_cap(self):
        updates = auth_retry.session_retry_updates(
            {CONF_AUTH_SESSION_LIMIT_ATTEMPTS: 5},
            AiguesApiSessionLimitError("Limit", retry_after=86400),
            now=1000,
        )
        self.assertEqual(updates[CONF_RETRY_NOT_BEFORE], 87400)

    def test_existing_longer_pause_is_never_shortened(self):
        updates = auth_retry.session_retry_updates(
            {CONF_RETRY_NOT_BEFORE: 100000}, AiguesApiSessionLimitError("Limit"), now=1000
        )
        self.assertEqual(updates[CONF_RETRY_NOT_BEFORE], 100000)

    def test_success_clears_only_expired_session_bookkeeping(self):
        data = {
            CONF_AUTH_SESSION_LIMIT: True,
            CONF_AUTH_SESSION_LIMIT_ATTEMPTS: 3,
            CONF_RETRY_NOT_BEFORE: 2000,
            "password": "saved",
        }
        with patch.object(auth_retry.time, "time", return_value=1000):
            self.assertEqual(auth_retry.clear_session_retry(data), {})
        with patch.object(auth_retry.time, "time", return_value=2000):
            self.assertEqual(
                auth_retry.clear_session_retry(data),
                {CONF_AUTH_SESSION_LIMIT: False, CONF_AUTH_SESSION_LIMIT_ATTEMPTS: 0},
            )
        self.assertEqual(data["password"], "saved")

    def test_remaining_pause_validates_storage_and_never_goes_negative(self):
        for value in (True, "1000", float("inf"), float("nan"), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                auth_retry.retry_delay({CONF_RETRY_NOT_BEFORE: value}, now=1000)
        self.assertEqual(auth_retry.retry_delay({CONF_RETRY_NOT_BEFORE: 1100}, now=1000), 100)
        self.assertEqual(auth_retry.retry_delay({CONF_RETRY_NOT_BEFORE: 999}, now=1000), 0)

    def test_malformed_attempt_count_and_retry_after_cannot_disable_cooldown(self):
        for count in (-1, True, "3", None):
            for delay in (-1, True, "1000", float("inf"), float("nan")):
                updates = auth_retry.session_retry_updates(
                    {CONF_AUTH_SESSION_LIMIT_ATTEMPTS: count},
                    AiguesApiSessionLimitError("Limit", retry_after=delay),
                    now=1000,
                )
                self.assertEqual(updates[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 1)
                self.assertEqual(updates[CONF_RETRY_NOT_BEFORE], 2800)
