"""Integration lifecycle and contract ownership for Aigües Barcelona History."""

from __future__ import annotations

from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_CONTRACT, DOMAIN
from .identity import canonical_contracts
from .service import async_setup as setup_service

PLATFORMS = [Platform.SENSOR, Platform.BUTTON, Platform.DATE]


def conflicting_contracts(hass: HomeAssistant, contracts, entry_id=None) -> set[str]:
    """Find contracts already reserved by another History entry or coordinator."""
    requested = set(canonical_contracts(contracts))
    registered = (
        contract
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != entry_id
        for contract in entry.data.get(CONF_CONTRACT, [])
    )
    running = (
        contract
        for contract, value in getattr(hass, "data", {}).get(DOMAIN, {}).items()
        if isinstance(value, dict)
        and (coordinator := value.get("coordinator")) is not None
        and coordinator.entry_id != entry_id
    )
    return requested & set(canonical_contracts((*registered, *running)))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Reject duplicate meter ownership before creating any platform objects."""
    if conflicts := conflicting_contracts(hass, entry.data[CONF_CONTRACT], entry.entry_id):
        raise HomeAssistantError(
            "History contracts already configured: " + ", ".join(sorted(conflicts))
        )
    from .sensor import AiguesHistoryCoordinator

    # Platforms are forwarded concurrently. Create their shared coordinators
    # before forwarding either platform, including on entry reload.
    entry.async_on_unload(partial(_remove_entry_data, hass, entry))
    entry.runtime_data = tuple(
        AiguesHistoryCoordinator(hass, entry, contract)
        for contract in canonical_contracts(entry.data[CONF_CONTRACT])
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await setup_service(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload this entry without removing another entry's coordinators or lock."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    _remove_entry_data(hass, entry)
    return True


def _remove_entry_data(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean only this entry's index after unload or failed platform setup."""
    entry.runtime_data = ()
    domain_data = hass.data.get(DOMAIN)
    if not domain_data:
        return
    contracts = [
        key
        for key, value in domain_data.items()
        if isinstance(value, dict)
        and getattr(value.get("coordinator"), "entry_id", None) == entry.entry_id
    ]
    for key in contracts:
        domain_data.pop(key, None)
    if not any(
        isinstance(value, dict) and "coordinator" in value for value in domain_data.values()
    ):
        hass.data.pop(DOMAIN, None)
