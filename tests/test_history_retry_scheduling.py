"""Exercise HA's real scheduler with History's session-limit recovery path."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from test_aigues_barcelona_history import OfflineTests

from custom_components.aigues_barcelona_history import auth_retry
from custom_components.aigues_barcelona_history.api import AiguesApiSessionLimitError
from custom_components.aigues_barcelona_history.const import (
    CONF_AUTH_SESSION_LIMIT,
    CONF_AUTH_SESSION_LIMIT_ATTEMPTS,
    CONF_RETRY_NOT_BEFORE,
)
from custom_components.aigues_barcelona_history.statistics import Frequency


class NativeRetrySchedulingTests(OfflineTests):
    async def test_session_pause_schedules_retry_then_success_restores_normal_interval(self):
        coordinator = self.coordinator
        entry = coordinator._entry()
        entry.pref_disable_polling = False
        entry.async_on_unload = Mock()
        entry.async_start_reauth_if_available = Mock()
        coordinator.hass.loop = loop = asyncio.get_running_loop()
        coordinator.hass.is_stopping = False

        # Keep the offline provider fixture, but initialize HA's actual
        # refresh/error handling, listeners, debouncer and timer scheduler.
        DataUpdateCoordinator.__init__(
            coordinator,
            coordinator.hass,
            logging.getLogger(__name__),
            config_entry=entry,
            name="History retry scheduling test",
            update_interval=timedelta(seconds=3900),
        )

        async def fetch_without_recorder(start, end, frequencies):
            await coordinator._request_consumptions(start, end, Frequency.DAILY)

        coordinator._import_range = fetch_without_recorder
        coordinator._recheck_incomplete_hours = AsyncMock()
        coordinator._api.consumptions.side_effect = AiguesApiSessionLimitError(
            "Session limit", error_code="MAX_SESSIONS"
        )
        remove_listener = coordinator.async_add_listener(Mock())
        try:
            with (
                patch.object(auth_retry.time, "time", return_value=1000),
                patch.object(loop, "call_at", wraps=loop.call_at) as schedule,
            ):
                before = loop.time()
                await coordinator._async_refresh(log_failures=False)
                after = loop.time()

            self.assertFalse(coordinator.last_update_success)
            self.assertIsInstance(coordinator.last_exception, UpdateFailed)
            self.assertEqual(coordinator.last_exception.retry_after, 1800)
            schedule.assert_called_once()
            retry_at = schedule.call_args.args[0]
            self.assertGreaterEqual(retry_at - before, 1799)
            self.assertLessEqual(retry_at - after, 1801)
            self.assertEqual(entry.data[CONF_RETRY_NOT_BEFORE], 2800)
            self.assertEqual(entry.data[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 1)
            self.assertTrue(entry.data[CONF_AUTH_SESSION_LIMIT])
            entry.async_start_reauth_if_available.assert_not_called()
            coordinator._api.login.assert_not_called()

            coordinator._api.consumptions.side_effect = None
            coordinator._api.consumptions.return_value = []
            with (
                patch.object(auth_retry.time, "time", return_value=2801),
                patch.object(loop, "call_at", wraps=loop.call_at) as schedule,
            ):
                before = loop.time()
                await coordinator._async_refresh(log_failures=False)
                after = loop.time()

            self.assertTrue(coordinator.last_update_success)
            schedule.assert_called_once()
            refresh_at = schedule.call_args.args[0]
            self.assertGreaterEqual(refresh_at - before, 3899)
            self.assertLessEqual(refresh_at - after, 3901)
            self.assertFalse(entry.data[CONF_AUTH_SESSION_LIMIT])
            self.assertEqual(entry.data[CONF_AUTH_SESSION_LIMIT_ATTEMPTS], 0)
            self.assertEqual(entry.data[CONF_RETRY_NOT_BEFORE], 0)
            coordinator._recheck_incomplete_hours.assert_awaited_once()
            entry.async_start_reauth_if_available.assert_not_called()
        finally:
            remove_listener()
            await coordinator.async_shutdown()
