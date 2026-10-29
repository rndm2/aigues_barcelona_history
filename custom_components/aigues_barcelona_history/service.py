"""Services for Aigües Barcelona History."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import CONF_CONTRACT, CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS, DOMAIN
from .identity import canonical_contracts
from .settings import validate_history_days

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_HISTORICAL_DATA = "import_historical_data"
SERVICE_RESET_AND_REFRESH_DATA = "reset_and_refresh_data"

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CONTRACT): str,
        vol.Optional(CONF_HISTORY_DAYS, default=DEFAULT_HISTORY_DAYS): validate_history_days,
        vol.Optional("frequency", default="ALL"): vol.In(["ALL", "DAILY", "HOURLY"]),
    }
)


async def async_run_import(hass, coordinator, *, frequency="ALL", days=None):
    """Give service and button imports the same entry-owned lifecycle."""
    entry = hass.config_entries.async_get_entry(coordinator.entry_id)
    if entry is None:
        raise HomeAssistantError("History config entry is no longer available")
    coroutine = (
        coordinator.import_configured_consumptions(frequency=frequency)
        if days is None
        else coordinator.import_old_consumptions(days=days, frequency=frequency)
    )
    task = entry.async_create_background_task(
        hass, coroutine, f"{DOMAIN} manual import {coordinator.contract} {frequency}"
    )
    await task


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration services."""

    async def _handle_history_import(
        call: ServiceCall, *, legacy_reset_alias: bool = False
    ) -> None:
        coordinator = _get_coordinator(hass, call.data.get(CONF_CONTRACT))
        if coordinator is None:
            raise HomeAssistantError("History contract coordinator not found")

        days = call.data.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        if legacy_reset_alias:
            _LOGGER.warning(
                "Legacy reset_and_refresh_data service called for %s. "
                "Statistics reset is not performed; importing %s historical days only.",
                coordinator.contract,
                days,
            )
        else:
            _LOGGER.warning(
                "Importing %s days of historical data for %s by service call",
                days,
                coordinator.contract,
            )

        await async_run_import(
            hass,
            coordinator,
            days=days,
            frequency=call.data.get("frequency", "ALL"),
        )

    async def handle_import_historical_data(call: ServiceCall) -> None:
        await _handle_history_import(call)

    async def handle_reset_and_refresh_data(call: ServiceCall) -> None:
        """Backward-compatible service name.

        The alias uses the same import path without clearing statistics.
        """
        await _handle_history_import(call, legacy_reset_alias=True)

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_HISTORICAL_DATA):
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_HISTORICAL_DATA,
            handle_import_historical_data,
            schema=SERVICE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RESET_AND_REFRESH_DATA):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESET_AND_REFRESH_DATA,
            handle_reset_and_refresh_data,
            schema=SERVICE_SCHEMA,
        )

    return True


def _get_coordinator(hass: HomeAssistant, contract: str | None):
    contracts = {
        key: value
        for key, value in hass.data.get(DOMAIN, {}).items()
        if isinstance(value, dict) and value.get("coordinator") is not None
    }

    if not contracts:
        _LOGGER.error("No contracts available")
        return None

    if contract:
        selected = contracts.get(canonical_contracts([contract])[0])
        if selected is None:
            _LOGGER.error("Contract coordinator for %s not found", contract)
            return None
    elif len(contracts) == 1:
        selected = next(iter(contracts.values()))
    else:
        _LOGGER.error(
            "Multiple contracts available. Provide a contract. Available: %s",
            ", ".join(sorted(contracts)),
        )
        return None

    coordinator = selected.get("coordinator")
    if coordinator is None:
        _LOGGER.error("Contract coordinator not found")
        return None

    return coordinator
