"""Services for Aigües Barcelona History."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import DateSelector
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import CONF_CONTRACT, CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS, DOMAIN
from .identity import canonical_contracts
from .settings import validate_history_days

_LOGGER = logging.getLogger(__name__)

SERVICE_IMPORT_HISTORICAL_DATA = "import_historical_data"
SERVICE_RESET_AND_REFRESH_DATA = "reset_and_refresh_data"
SERVICE_IMPORT_HOURLY_DATE_RANGE = "import_hourly_date_range"

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CONTRACT): str,
        vol.Optional(CONF_HISTORY_DAYS, default=DEFAULT_HISTORY_DAYS): validate_history_days,
        vol.Optional("frequency", default="ALL"): vol.In(["ALL", "DAILY", "HOURLY"]),
    }
)

HOURLY_DATE_RANGE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CONTRACT): str,
        vol.Required("start_date"): DateSelector(),
        vol.Required("end_date"): DateSelector(),
    }
)


def manual_import_available(coordinator, *, allow_during_import: bool = False) -> bool:
    """Keep manual recovery controls usable after a provider refresh failure."""
    return not (
        coordinator._stopping
        or getattr(coordinator, "_shutdown_requested", False)
        or (not allow_during_import and getattr(coordinator, "import_in_progress", False))
    )


async def async_run_import(
    hass, coordinator, *, frequency="ALL", days=None, start_date=None, end_date=None
):
    """Give service and button imports the same entry-owned lifecycle."""
    date_range = start_date is not None or end_date is not None
    if date_range and (start_date is None or end_date is None):
        raise HomeAssistantError("Select both hourly import reporting dates")
    if date_range and (days is not None or frequency not in ("ALL", "HOURLY")):
        raise HomeAssistantError("A reporting-date range imports hourly history only")
    entry = hass.config_entries.async_get_entry(coordinator.entry_id)
    if entry is None:
        raise HomeAssistantError("History config entry is no longer available")
    coordinator._set_manual_import_reserved(True)
    if date_range:
        frequency = "HOURLY"
        coroutine = coordinator.import_hourly_date_range(start_date, end_date)
    elif days is None:
        coroutine = coordinator.import_configured_consumptions(frequency=frequency)
    else:
        coroutine = coordinator.import_old_consumptions(days=days, frequency=frequency)
    task = None
    try:
        task = entry.async_create_background_task(
            hass, coroutine, f"{DOMAIN} manual import {coordinator.contract} {frequency}"
        )
        await task
    finally:
        if task is None:
            coroutine.close()
        coordinator._set_manual_import_reserved(False)


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

    async def handle_import_hourly_date_range(call: ServiceCall) -> None:
        """Import both selected provider reporting dates and every date between."""
        coordinator = _get_coordinator(hass, call.data.get(CONF_CONTRACT))
        if coordinator is None:
            raise HomeAssistantError("History contract coordinator not found")
        await async_run_import(
            hass,
            coordinator,
            frequency="HOURLY",
            start_date=call.data["start_date"],
            end_date=call.data["end_date"],
        )

    for name, handler, schema in (
        (SERVICE_IMPORT_HISTORICAL_DATA, handle_import_historical_data, SERVICE_SCHEMA),
        (SERVICE_RESET_AND_REFRESH_DATA, handle_reset_and_refresh_data, SERVICE_SCHEMA),
        (
            SERVICE_IMPORT_HOURLY_DATE_RANGE,
            handle_import_hourly_date_range,
            HOURLY_DATE_RANGE_SCHEMA,
        ),
    ):
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(DOMAIN, name, handler, schema=schema)

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
