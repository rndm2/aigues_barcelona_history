"""Expose operational diagnostics without account or consumption data."""

from __future__ import annotations

from collections.abc import Mapping
import math
from time import time
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from voluptuous import Invalid

from .const import CONF_AUTH_SESSION_LIMIT, CONF_RETRY_NOT_BEFORE
from .settings import effective_settings
from .statistics import Frequency
from .version import VERSION


def _retry_pending(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > time()
    except OverflowError:
        return False


def _interval_counts(ledger: Any) -> dict[str, int | None]:
    """Count saved intervals without reading keys or measurement values."""
    return {
        frequency.value.lower(): (
            len(intervals)
            if isinstance(ledger, Mapping)
            and isinstance(intervals := ledger.get(frequency.value), Mapping)
            else None
        )
        for frequency in Frequency
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return only approved settings, counts and lifecycle status.

    Never serialize the entry or coordinator data. Those objects contain
    identifiers, credentials, meter values, timestamps and cyclic references.
    """
    try:
        state = ConfigEntryState(entry.state).value
    except TypeError, ValueError:
        state = "unknown"
    try:
        settings = effective_settings(entry)
    except Invalid:
        settings = None

    meters = []
    for coordinator in getattr(entry, "runtime_data", ()):
        ledger = getattr(coordinator, "_ledger", None)
        meters.append(
            {
                "available": getattr(coordinator, "last_update_success", False) is True,
                "stopping": (
                    getattr(coordinator, "_stopping", False) is True
                    or getattr(coordinator, "_shutdown_requested", False) is True
                ),
                "startup_import_pending": (
                    getattr(coordinator, "_should_import_history", False) is True
                ),
                "ledger_loaded": ledger is not None,
                "saved_intervals": _interval_counts(ledger),
            }
        )

    return {
        "integration_version": VERSION,
        "entry_state": state,
        "configuration_valid": settings is not None,
        "settings": settings,
        "authentication": {
            "session_limit_blocked": entry.data.get(CONF_AUTH_SESSION_LIMIT) is True,
            "retry_pending": _retry_pending(entry.data.get(CONF_RETRY_NOT_BEFORE)),
        },
        "meters": meters,
    }
