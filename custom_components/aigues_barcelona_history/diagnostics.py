"""Expose operational diagnostics without account or consumption data."""

from __future__ import annotations

from collections.abc import Mapping
import math
from time import time
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from voluptuous import Invalid

from .const import CONF_AUTH_SESSION_LIMIT, CONF_RETRY_NOT_BEFORE, IMPORT_PHASES
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


def _import_progress(data: Any) -> dict:
    """Allow operational progress only, excluding window dates and error text."""
    status = data.get("import_status") if isinstance(data, Mapping) else None
    status = status if isinstance(status, Mapping) else {}
    phase = status.get("phase")
    frequency = status.get("frequency")
    progress = {
        "phase": phase if isinstance(phase, str) and phase in IMPORT_PHASES else "unknown",
        "frequency": frequency if isinstance(frequency, str) and frequency in Frequency else None,
        "has_error": isinstance(status.get("last_error"), str) and bool(status["last_error"]),
    }
    for key in ("completed_windows", "total_windows"):
        value = status.get(key)
        progress[key] = value if type(value) is int and value >= 0 else None
    return progress


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
                "import_status": _import_progress(getattr(coordinator, "_data", None)),
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
