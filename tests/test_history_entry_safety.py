"""Offline entry, form and task ownership checks without paid authentication."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components import aigues_barcelona_history as integration
from custom_components.aigues_barcelona_history import config_flow, sensor, service
from custom_components.aigues_barcelona_history.api import (
    AiguesApiAuthError,
    AiguesApiServerError,
    AiguesApiSessionLimitError,
)
from custom_components.aigues_barcelona_history.const import (
    CONF_2CAPTCHA_APIKEY,
    CONF_AUTH_SESSION_LIMIT,
    CONF_CONTRACT,
    CONF_RETRY_NOT_BEFORE,
    CONF_SCAN_PERIOD,
    DOMAIN,
)

USER = "X1234567L"
CREDENTIALS = {CONF_USERNAME: USER, CONF_PASSWORD: "unused", CONF_2CAPTCHA_APIKEY: "unused"}
INFO = {
    CONF_CONTRACT: ["ONE", "TWO"],
    CONF_TOKEN: "fresh-token",
    CONF_AUTH_SESSION_LIMIT: False,
    CONF_RETRY_NOT_BEFORE: 0,
}


class EntrySafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(
            patch("requests.Session.request", side_effect=AssertionError("Network forbidden"))
        )
        self.enterContext(
            patch("socket.create_connection", side_effect=AssertionError("Network forbidden"))
        )
        self.api = Mock()
        self.api.login.return_value = True
        self.api.contracts.return_value = [
            {"contractDetail": {"contractNumber": contract}} for contract in INFO[CONF_CONTRACT]
        ]
        self.api.get_token.return_value = INFO[CONF_TOKEN]
        self.api.is_token_expired.return_value = False
        self.api._return_token_field.return_value = USER
        self.client = self.enterContext(
            patch.object(config_flow, "AiguesBarcelonaHistoryApiClient", return_value=self.api)
        )
        self.entry = SimpleNamespace(
            entry_id="history-entry",
            domain=DOMAIN,
            title="History account",
            data={
                **CREDENTIALS,
                CONF_TOKEN: "expired-token",
                CONF_CONTRACT: ["ONE"],
                CONF_AUTH_SESSION_LIMIT: False,
                CONF_RETRY_NOT_BEFORE: 123,
            },
            options={},
            unique_id=f"{DOMAIN}_{USER}",
            update_listeners=[],
            async_on_unload=Mock(),
        )

        async def execute(function, *args):
            return function(*args)

        def update_entry(entry, **changes):
            for key, value in changes.items():
                setattr(entry, key, value)
            return True

        self.entries = SimpleNamespace(
            async_entries=Mock(return_value=[]),
            async_entry_for_domain_unique_id=Mock(return_value=None),
            async_get_known_entry=Mock(return_value=self.entry),
            async_update_entry=Mock(side_effect=update_entry),
            async_schedule_reload=Mock(),
            async_forward_entry_setups=AsyncMock(),
        )
        self.old_store = {"sentinel": object()}
        self.hass = SimpleNamespace(
            config_entries=self.entries,
            async_add_executor_job=execute,
            data={"aigues_barcelona": self.old_store},
        )

    def flow(self, source="user"):
        flow = config_flow.AiguesBarcelonaHistoryConfigFlow()
        flow.hass = self.hass
        flow.handler, flow.flow_id = DOMAIN, "offline-flow"
        flow.context = {"source": source}
        if source == "reauth":
            flow.context["entry_id"] = self.entry.entry_id
        flow._async_in_progress = Mock(return_value=[])
        return flow

    async def test_opening_reauth_only_displays_form_without_validation(self):
        flow = self.flow("reauth")
        with patch.object(config_flow, "validate_credentials", new_callable=AsyncMock) as validate:
            result = await flow.async_step_reauth(self.entry.data)
            validate.assert_not_awaited()
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "reauth_confirm")
        self.api.login.assert_not_called()

    async def test_opening_blocked_reauth_explains_session_limit_without_login(self):
        self.entry.data[CONF_AUTH_SESSION_LIMIT] = True
        result = await self.flow("reauth").async_step_reauth(self.entry.data)
        self.assertEqual(result["errors"], {"base": CONF_AUTH_SESSION_LIMIT})
        self.api.login.assert_not_called()

    async def test_submitted_reauth_validates_once_and_preserves_selected_contracts(self):
        flow = self.flow("reauth")
        await flow.async_step_reauth(self.entry.data)
        flow.async_update_reload_and_abort = Mock(
            return_value={"type": "abort", "reason": "reauth_successful"}
        )
        info = {**INFO, CONF_CONTRACT: ["TWO", "ONE", "NEW"]}
        with patch.object(
            config_flow, "validate_credentials", AsyncMock(return_value=info)
        ) as validate:
            result = await flow.async_step_reauth_confirm({})
        validate.assert_awaited_once()
        self.assertNotIn(CONF_TOKEN, validate.await_args.args[1])
        updates = flow.async_update_reload_and_abort.call_args.kwargs["data_updates"]
        self.assertEqual(updates[CONF_CONTRACT], ["ONE"])
        self.assertFalse(updates[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(updates[CONF_RETRY_NOT_BEFORE], 0)
        self.assertEqual(result["reason"], "reauth_successful")
        self.assertEqual(self.entry.unique_id, f"{DOMAIN}_{USER}")

    async def test_changed_reauth_account_is_rejected_before_validation(self):
        flow = self.flow("reauth")
        await flow.async_step_reauth(self.entry.data)
        with patch.object(config_flow, "validate_credentials", new_callable=AsyncMock) as validate:
            result = await flow.async_step_reauth_confirm({CONF_USERNAME: "Y1234567L"})
            validate.assert_not_awaited()
        self.assertEqual(result["errors"], {"base": "invalid_auth"})

    async def test_missing_configured_contract_is_rejected_without_entry_update(self):
        flow = self.flow("reauth")
        await flow.async_step_reauth(self.entry.data)
        with patch.object(
            config_flow,
            "validate_credentials",
            AsyncMock(return_value={**INFO, CONF_CONTRACT: ["OTHER"]}),
        ):
            result = await flow.async_step_reauth_confirm({})
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.entries.async_update_entry.assert_not_called()

    async def test_duplicate_account_with_different_case_is_rejected_before_login(self):
        self.entries.async_entries.return_value = [self.entry]
        with patch.object(config_flow, "validate_credentials", new_callable=AsyncMock) as validate:
            result = await self.flow().async_step_user({**CREDENTIALS, CONF_USERNAME: USER.lower()})
            validate.assert_not_awaited()
        self.assertEqual(result["reason"], "already_configured")
        self.api.login.assert_not_called()

    async def test_new_account_unique_id_is_canonical_without_touching_old_domain(self):
        with patch.object(config_flow, "validate_credentials", AsyncMock(return_value=INFO)):
            flow = self.flow()
            result = await flow.async_step_user({**CREDENTIALS, CONF_USERNAME: USER.lower()})
            self.assertEqual(result["step_id"], "import_settings")
            result = await flow.async_step_import_settings({})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(flow.unique_id, f"{DOMAIN}_{USER}")
        self.assertEqual(result["data"][CONF_USERNAME], USER)
        self.assertIs(self.hass.data["aigues_barcelona"], self.old_store)
        self.assertTrue(
            all(call.args == (DOMAIN,) for call in self.entries.async_entries.call_args_list)
        )

    async def test_manual_token_uses_only_contract_get_and_clears_auth_blocks(self):
        info = await config_flow.validate_credentials(
            self.hass, {CONF_USERNAME: USER, CONF_TOKEN: "fresh-token"}
        )
        self.api.set_token.assert_called_once_with("fresh-token")
        self.api.login.assert_not_called()
        self.api.contracts.assert_called_once_with(USER)
        self.assertFalse(info[CONF_AUTH_SESSION_LIMIT])
        self.assertEqual(info[CONF_RETRY_NOT_BEFORE], 0)

    async def test_wrong_or_missing_token_account_is_rejected_before_get_or_login(self):
        for claim in ("Y1234567L", None, 123):
            with self.subTest(claim=claim):
                self.api._return_token_field.return_value = claim
                with self.assertRaises(config_flow.InvalidAuth):
                    await config_flow.validate_credentials(
                        self.hass, {CONF_USERNAME: USER, CONF_TOKEN: "other-token"}
                    )
                self.api.contracts.assert_not_called()
                self.api.login.assert_not_called()

    async def test_manual_token_account_comparison_is_case_insensitive(self):
        self.api._return_token_field.return_value = USER.lower()
        info = await config_flow.validate_credentials(
            self.hass, {CONF_USERNAME: USER.lower(), CONF_TOKEN: "fresh-token"}
        )
        self.assertEqual(info[CONF_TOKEN], "fresh-token")
        self.api.contracts.assert_called_once_with(USER)
        self.api.login.assert_not_called()

    async def test_legacy_token_step_validates_token_without_paid_login(self):
        flow = self.flow()
        flow.stored_input = dict(CREDENTIALS)
        result = await flow.async_step_token({CONF_TOKEN: "fresh-token"})
        self.assertEqual(result["step_id"], "import_settings")
        result = await flow.async_step_import_settings({})
        self.assertEqual(result["type"], "create_entry")
        self.api.login.assert_not_called()
        self.api.contracts.assert_called_once_with(USER)

    async def test_token_only_step_never_falls_back_to_login_for_blank_input(self):
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                flow = self.flow()
                flow.stored_input = dict(CREDENTIALS)
                result = await flow.async_step_token({CONF_TOKEN: value})
                self.assertEqual(result["errors"], {"base": "invalid_auth"})
                self.api.login.assert_not_called()
                self.api.contracts.assert_not_called()

    async def test_whitespace_token_is_rejected_without_using_supplied_credentials(self):
        with self.assertRaises(config_flow.InvalidAuth):
            await config_flow.validate_credentials(self.hass, {**CREDENTIALS, CONF_TOKEN: "   "})
        self.api.login.assert_not_called()
        self.api.contracts.assert_not_called()

    async def test_expired_explicit_token_does_not_fall_back_to_paid_login(self):
        self.api.is_token_expired.return_value = True
        with self.assertRaises(config_flow.TokenExpired):
            await config_flow.validate_credentials(
                self.hass, {CONF_USERNAME: USER, CONF_TOKEN: "expired-token"}
            )
        self.api.login.assert_not_called()
        self.api.contracts.assert_not_called()

    async def test_server_errors_are_connection_errors_instead_of_bad_credentials(self):
        self.api.login.side_effect = AiguesApiServerError("temporary failure")
        result = await self.flow().async_step_user(CREDENTIALS)
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.api.login.assert_called_once_with()

    async def test_login_without_a_usable_token_is_not_reported_as_bad_credentials(self):
        self.api.login.return_value = False
        result = await self.flow().async_step_user(CREDENTIALS)
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.api.login.assert_called_once_with()

    async def test_captcha_service_failure_is_a_form_error_without_retry(self):
        self.api.login.side_effect = RuntimeError("2Captcha request failed")
        result = await self.flow().async_step_user(CREDENTIALS)
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.api.login.assert_called_once_with()

    async def test_missing_returned_login_token_is_a_connection_error(self):
        self.api.get_token.return_value = None
        result = await self.flow().async_step_user(CREDENTIALS)
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.api.login.assert_called_once_with()

    async def test_known_captcha_errors_are_safe_form_errors_without_extra_attempts(self):
        for exception in (
            config_flow.captcha_api.NetworkException,
            config_flow.captcha_api.ApiException,
        ):
            with self.subTest(exception=exception.__name__):
                self.api.login.reset_mock()
                self.api.login.side_effect = exception("PRIVATE_SOLVER_PAYLOAD")
                result = await self.flow().async_step_user(CREDENTIALS)
                self.assertEqual(result["errors"], {"base": "cannot_connect"})
                self.assertNotIn("PRIVATE_SOLVER_PAYLOAD", repr(result))
                self.api.login.assert_called_once_with()

    async def test_explicit_auth_rejection_is_bad_credentials(self):
        self.api.login.side_effect = AiguesApiAuthError("denied")
        result = await self.flow().async_step_user(CREDENTIALS)
        self.assertEqual(result["errors"], {"base": "invalid_auth"})

    async def test_session_limit_is_visible_and_persisted_for_existing_entry(self):
        self.api.login.side_effect = AiguesApiSessionLimitError("session limit")
        flow = self.flow("reauth")
        await flow.async_step_reauth(self.entry.data)
        result = await flow.async_step_reauth_confirm({})
        self.assertEqual(result["errors"], {"base": CONF_AUTH_SESSION_LIMIT})
        self.assertTrue(self.entry.data[CONF_AUTH_SESSION_LIMIT])
        self.api.login.assert_called_once_with()

    async def test_new_entry_cannot_claim_contract_reserved_by_another_entry(self):
        self.entries.async_entries.return_value = [
            SimpleNamespace(
                entry_id="other-entry", data={CONF_USERNAME: "Y1234567L", CONF_CONTRACT: ["ONE"]}
            )
        ]
        with patch.object(config_flow, "validate_credentials", AsyncMock(return_value=INFO)):
            result = await self.flow().async_step_user(CREDENTIALS)
        self.assertEqual(result["reason"], "contract_already_configured")

    async def test_contract_canonicalization_preserves_order_without_case_duplicates(self):
        self.assertEqual(
            integration.canonical_contracts(["first", "SECOND", "FIRST", "second"]),
            ["FIRST", "SECOND"],
        )

    async def test_validation_deduplicates_case_variant_provider_contracts(self):
        self.api.contracts.return_value = [
            {"contractDetail": {"contractNumber": contract}}
            for contract in ("first", "SECOND", "FIRST")
        ]
        info = await config_flow.validate_credentials(
            self.hass, {CONF_USERNAME: USER, CONF_TOKEN: "fresh-token"}
        )
        self.assertEqual(info[CONF_CONTRACT], ["FIRST", "SECOND"])
        self.api.login.assert_not_called()

    async def test_reauth_case_matching_preserves_original_configured_contract_list(self):
        self.entry.data[CONF_CONTRACT] = ["one"]
        flow = self.flow("reauth")
        await flow.async_step_reauth(self.entry.data)
        flow.async_update_reload_and_abort = Mock(return_value={"type": "abort"})
        with patch.object(config_flow, "validate_credentials", AsyncMock(return_value=INFO)):
            await flow.async_step_reauth_confirm({})
        updates = flow.async_update_reload_and_abort.call_args.kwargs["data_updates"]
        self.assertEqual(updates[CONF_CONTRACT], ["one"])

    async def test_setup_rejects_registered_contract_collision_before_platform_creation(self):
        self.entries.async_entries.return_value = [
            self.entry,
            SimpleNamespace(entry_id="other-entry", data={CONF_CONTRACT: ["one"]}),
        ]
        with self.assertRaises(HomeAssistantError):
            await integration.async_setup_entry(self.hass, self.entry)
        self.entries.async_forward_entry_setups.assert_not_awaited()
        self.assertIs(self.hass.data["aigues_barcelona"], self.old_store)

    async def test_setup_rejects_runtime_owner_collision_before_platform_creation(self):
        self.hass.data[DOMAIN] = {"ONE": {"coordinator": SimpleNamespace(entry_id="other-entry")}}
        with self.assertRaises(HomeAssistantError):
            await integration.async_setup_entry(self.hass, self.entry)
        self.entries.async_forward_entry_setups.assert_not_awaited()

    async def test_own_entry_does_not_conflict_and_setup_uses_no_update_listener(self):
        self.entries.async_entries.return_value = [self.entry]
        with (
            patch.object(integration, "setup_service", new_callable=AsyncMock) as setup,
            patch.object(sensor, "AiguesHistoryCoordinator"),
        ):
            await integration.async_setup_entry(self.hass, self.entry)
        self.entries.async_forward_entry_setups.assert_awaited_once()
        setup.assert_awaited_once_with(self.hass, self.entry)
        self.assertEqual(self.entry.update_listeners, [])

    async def test_options_flow_reloads_only_when_ha_finishes_changed_options(self):
        self.assertTrue(
            issubclass(
                config_flow.AiguesBarcelonaHistoryOptionsFlow, config_entries.OptionsFlowWithReload
            )
        )
        manager = object.__new__(config_entries.OptionsFlowManager)
        manager.hass = self.hass
        flow = config_flow.AiguesBarcelonaHistoryOptionsFlow()
        flow.handler = self.entry.entry_id
        result = {"type": FlowResultType.CREATE_ENTRY, "data": {CONF_SCAN_PERIOD: 7200}}
        await manager.async_finish_flow(flow, result)
        self.entries.async_schedule_reload.assert_called_once_with(self.entry.entry_id)
        self.entries.async_schedule_reload.reset_mock()
        self.entries.async_update_entry.return_value = False
        self.entries.async_update_entry.side_effect = None
        await manager.async_finish_flow(flow, result)
        self.entries.async_schedule_reload.assert_not_called()

    async def test_manual_service_import_task_is_owned_and_cancelled_on_unload(self):
        entered, release = asyncio.Event(), asyncio.Event()
        tracked, finished = [], []

        async def import_data(**kwargs):
            entered.set()
            await release.wait()
            finished.append(True)

        coordinator = SimpleNamespace(
            contract="ONE",
            entry_id=self.entry.entry_id,
            import_old_consumptions=AsyncMock(side_effect=import_data),
            _set_manual_import_reserved=Mock(),
        )

        def create_background(hass, coroutine, name):
            task = asyncio.create_task(coroutine)
            tracked.append(task)
            return task

        self.entry.async_create_background_task = Mock(side_effect=create_background)
        self.entries.async_get_entry = Mock(return_value=self.entry)
        self.hass.data[DOMAIN] = {"ONE": {"coordinator": coordinator}}
        handlers = {}
        self.hass.services = SimpleNamespace(
            has_service=lambda domain, name: False,
            async_register=lambda domain, name, handler, **kwargs: handlers.__setitem__(
                (domain, name), handler
            ),
        )
        await service.async_setup(self.hass, {})
        call = SimpleNamespace(data={CONF_CONTRACT: "ONE", "history_days": 7, "frequency": "ALL"})
        request = asyncio.create_task(handlers[(DOMAIN, "import_historical_data")](call))
        await entered.wait()
        self.assertEqual(len(tracked), 1)
        tracked[0].cancel("entry unloaded")
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(finished, [])

    async def test_missing_entry_rejects_service_before_creating_import_coroutine(self):
        coordinator = SimpleNamespace(
            contract="ONE", entry_id=self.entry.entry_id, import_old_consumptions=AsyncMock()
        )
        self.hass.data[DOMAIN] = {"ONE": {"coordinator": coordinator}}
        self.entries.async_get_entry = Mock(return_value=None)
        handlers = {}
        self.hass.services = SimpleNamespace(
            has_service=lambda domain, name: False,
            async_register=lambda domain, name, handler, **kwargs: handlers.__setitem__(
                (domain, name), handler
            ),
        )
        await service.async_setup(self.hass, {})
        with self.assertRaises(HomeAssistantError):
            await handlers[(DOMAIN, "import_historical_data")](SimpleNamespace(data={}))
        coordinator.import_old_consumptions.assert_not_called()


if __name__ == "__main__":
    unittest.main()
