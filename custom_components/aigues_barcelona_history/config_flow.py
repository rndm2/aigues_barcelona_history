"""Configure History and recover authentication only after form submission."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from requests.exceptions import RequestException
from twocaptcha import api as captcha_api
import voluptuous as vol

from . import conflicting_contracts
from .api import (
    AiguesApiAuthError,
    AiguesApiError,
    AiguesApiSessionLimitError,
    AiguesBarcelonaHistoryApiClient,
)
from .const import (
    CONF_2CAPTCHA_APIKEY,
    CONF_AUTH_SESSION_LIMIT,
    CONF_CONTRACT,
    CONF_RETRY_NOT_BEFORE,
    DOMAIN,
    NAME,
)
from .identity import (
    canonical_account as _account,
)
from .identity import (
    contracts_from_response,
    contracts_include,
    token_matches_account,
)
from .settings import (
    effective_settings,
    new_entry_settings,
    options_from_input,
    settings_schema,
)

ACCOUNT_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_2CAPTCHA_APIKEY): cv.string,
        vol.Optional(CONF_TOKEN): cv.string,
    }
)
REAUTH_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_2CAPTCHA_APIKEY): cv.string,
        vol.Optional(CONF_TOKEN): cv.string,
    }
)
TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): cv.string})


def check_valid_nif(username: str) -> bool:
    """Accept the existing personal DNI/NIE account formats."""
    username = _account(username)
    return len(username) == 9 and (
        (username[:8].isnumeric() and username[-1].isalpha())
        or (username[0] in "XYZ" and username[1:8].isnumeric() and username[-1].isalpha())
    )


async def validate_credentials(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate one submitted sign-in or an existing token without paid login."""
    username = _account(data[CONF_USERNAME])
    if not check_valid_nif(username):
        raise InvalidUsername
    api = AiguesBarcelonaHistoryApiClient(
        username, data.get(CONF_PASSWORD, ""), data.get(CONF_2CAPTCHA_APIKEY, "")
    )
    try:
        raw_token = data.get(CONF_TOKEN, "")
        if raw_token and not raw_token.strip():
            raise InvalidAuth
        if token := raw_token.strip():
            api.set_token(token)
            if not token_matches_account(api, username):
                raise InvalidAuth
            # The claim is an identity hint; the authenticated GET below still
            # validates the token with the provider.
            try:
                expired = api.is_token_expired()
            except (ValueError, TypeError, IndexError) as err:
                raise InvalidAuth from err
            if expired:
                raise TokenExpired
        else:
            if not data.get(CONF_PASSWORD) or not data.get(CONF_2CAPTCHA_APIKEY):
                raise InvalidAuth
            if not await hass.async_add_executor_job(api.login):
                raise CannotConnect
        contracts = await hass.async_add_executor_job(api.contracts, username)
        token = api.get_token()
        if not token or not isinstance(contracts, list):
            raise CannotConnect
        available = contracts_from_response(contracts)
        if not available:
            raise NoContracts
        return {
            CONF_CONTRACT: available,
            CONF_TOKEN: token,
            CONF_AUTH_SESSION_LIMIT: False,
            CONF_RETRY_NOT_BEFORE: 0,
        }
    except AiguesApiSessionLimitError:
        raise
    except AiguesApiAuthError as err:
        raise InvalidAuth from err
    except (
        AiguesApiError,
        RequestException,
        captcha_api.NetworkException,
        captcha_api.ApiException,
        RuntimeError,
        KeyError,
        TypeError,
        ValueError,
    ) as err:
        raise CannotConnect from err


class AiguesBarcelonaHistoryOptionsFlow(config_entries.OptionsFlowWithReload):
    """Apply import and Energy display options without reacting to token persistence."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        values = {**self.config_entry.options, **effective_settings(self.config_entry)}
        errors = {}
        if user_input is not None:
            try:
                return self.async_create_entry(
                    data=options_from_input(user_input, existing_options=values)
                )
            except vol.Invalid:
                errors["base"] = "invalid_settings"
                values.update(user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=settings_schema(values),
            errors=errors,
        )


class AiguesBarcelonaHistoryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create independent entries and recover their original account identity."""

    VERSION = 2

    def __init__(self) -> None:
        self.stored_input: dict[str, Any] = {}
        self.entry: config_entries.ConfigEntry | None = None
        self._validated_input: dict[str, Any] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return AiguesBarcelonaHistoryOptionsFlow()

    def _form(self, step_id: str, errors: dict | None = None) -> FlowResult:
        schemas = {
            "user": ACCOUNT_CONFIG_SCHEMA,
            "reauth_confirm": REAUTH_CONFIG_SCHEMA,
            "token": TOKEN_SCHEMA,
        }
        return self.async_show_form(
            step_id=step_id, data_schema=schemas[step_id], errors=errors or {}
        )

    async def async_step_reauth(self, entry_data) -> FlowResult:
        """Present recovery without performing any authentication request."""
        self.entry = self._get_reauth_entry()
        self.stored_input = dict(self.entry.data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> FlowResult:
        if user_input is None:
            errors = (
                {"base": CONF_AUTH_SESSION_LIMIT}
                if self.entry and self.entry.data.get(CONF_AUTH_SESSION_LIMIT)
                else {}
            )
            return self._form("reauth_confirm", errors)
        return await self._process_input(user_input, "reauth_confirm")

    async def async_step_token(self, user_input=None) -> FlowResult:
        """Validate the explicitly supplied token with a read-only contracts GET."""
        if user_input is None:
            return self._form("token")
        if not user_input.get(CONF_TOKEN, "").strip():
            return self._form("token", {"base": "invalid_auth"})
        return await self._process_input(user_input, "token")

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self._form("user")
        return await self._process_input(user_input, "user")

    def _account_configured(self, username: str) -> bool:
        return any(
            _account(entry.data.get(CONF_USERNAME, "")) == username
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    async def async_step_import_settings(self, user_input=None) -> FlowResult:
        """Choose imports and Energy coverage after authentication, without another login."""
        if self._validated_input is None:
            return await self.async_step_user()
        values = new_entry_settings()
        errors = {}
        if user_input is not None:
            try:
                options = options_from_input(user_input, existing_options=values)
            except vol.Invalid:
                errors["base"] = "invalid_settings"
                values.update(user_input)
            else:
                data = self._validated_input
                # Another flow may have completed while this form was open.
                if self._account_configured(data[CONF_USERNAME]):
                    return self.async_abort(reason="already_configured")
                self._abort_if_unique_id_configured()
                if conflicting_contracts(self.hass, data[CONF_CONTRACT]):
                    return self.async_abort(reason="contract_already_configured")
                return self.async_create_entry(
                    title=f"{NAME} ****{data[CONF_USERNAME][-3:-1]}",
                    data=data,
                    options=options,
                )
        return self.async_show_form(
            step_id="import_settings", data_schema=settings_schema(values), errors=errors
        )

    async def _process_input(self, user_input: dict, step_id: str) -> FlowResult:
        # Do not silently reuse the expired saved token for a credential retry.
        data = {key: value for key, value in self.stored_input.items() if key != CONF_TOKEN}
        data.update(user_input)
        data.setdefault(CONF_PASSWORD, "")
        data.setdefault(CONF_2CAPTCHA_APIKEY, "")
        errors = {}
        try:
            data[CONF_USERNAME] = _account(data[CONF_USERNAME])
            if self.entry is not None:
                if data[CONF_USERNAME] != _account(self.entry.data[CONF_USERNAME]):
                    raise InvalidAuth
            else:
                # Avoid paying for an account that is already configured, including
                # pre-existing entries whose unique ID used a different letter case.
                if self._account_configured(data[CONF_USERNAME]):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(f"{DOMAIN}_{data[CONF_USERNAME]}")
                self._abort_if_unique_id_configured()
            self.stored_input = dict(data)
            info = await validate_credentials(self.hass, data)
            configured = self.entry.data[CONF_CONTRACT] if self.entry else info[CONF_CONTRACT]
            if not contracts_include(info[CONF_CONTRACT], configured):
                raise InvalidAuth
            if conflicting_contracts(
                self.hass, configured, self.entry.entry_id if self.entry else None
            ):
                return self.async_abort(reason="contract_already_configured")
            if self.entry is not None:
                return self.async_update_reload_and_abort(
                    self.entry,
                    data_updates={**data, **info, CONF_CONTRACT: configured},
                )
            self._validated_input = {**data, **info}
            return await self.async_step_import_settings()
        except AiguesApiSessionLimitError:
            errors["base"] = CONF_AUTH_SESSION_LIMIT
            if self.entry is not None:
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, CONF_AUTH_SESSION_LIMIT: True},
                )
        except TokenExpired:
            errors["base"] = "token_expired"
        except InvalidAuth, InvalidUsername:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except NoContracts:
            errors["base"] = "no_contracts"
        return self._form(step_id, errors)


class InvalidAuth(HomeAssistantError):
    """Credentials or a supplied token were rejected."""


class InvalidUsername(InvalidAuth):
    """The personal account identifier is invalid."""


class TokenExpired(HomeAssistantError):
    """An explicitly supplied token has expired."""


class CannotConnect(HomeAssistantError):
    """Provider or network data could not be validated."""


class NoContracts(HomeAssistantError):
    """The account has no available water contracts."""
