"""Shared import settings, validation and friendly HA form selectors."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorMode,
)
import voluptuous as vol

from .const import (
    CONF_HISTORY_DAYS,
    CONF_HOURLY_HISTORY_DAYS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SCAN_PERIOD,
    CONF_SHOULD_IMPORT_HISTORY,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_PERIOD,
    MAX_HISTORY_DAYS,
    MAX_SCAN_PERIOD,
    MIN_SCAN_PERIOD,
)


def _whole_number(value, field: str, minimum: int, maximum: int) -> int:
    """Accept HA selector floats without silently rounding fractional inputs."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise vol.Invalid(f"{field} must be a whole number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise vol.Invalid(f"Invalid {field}") from error
    if (
        not number.is_finite()
        or number != number.to_integral_value()
        or not minimum <= number <= maximum
    ):
        raise vol.Invalid(f"{field} must be between {minimum} and {maximum}")
    return int(number)


def validate_history_days(value) -> int:
    """Use the same day limit for setup, options, buttons and service imports."""
    return _whole_number(value, "History days", 1, MAX_HISTORY_DAYS)


def _validate_scan_period(value) -> int:
    return _whole_number(value, "Refresh interval", MIN_SCAN_PERIOD, MAX_SCAN_PERIOD)


def effective_settings(entry) -> dict:
    """Resolve legacy entries without changing their saved data or pending run."""
    values = {**entry.data, **entry.options}
    daily_days = validate_history_days(values.get(CONF_HISTORY_DAYS, MAX_HISTORY_DAYS))
    pending = values.get(CONF_SHOULD_IMPORT_HISTORY, False)
    if not isinstance(pending, bool):
        raise vol.Invalid("Import after saving must be a boolean")
    return {
        CONF_HISTORY_DAYS: daily_days,
        CONF_HOURLY_HISTORY_DAYS: validate_history_days(
            values.get(CONF_HOURLY_HISTORY_DAYS, daily_days)
        ),
        CONF_SCAN_PERIOD: _validate_scan_period(values.get(CONF_SCAN_PERIOD, DEFAULT_SCAN_PERIOD)),
        CONF_SHOULD_IMPORT_HISTORY: pending,
    }


def new_entry_settings() -> dict:
    """Offer explicit initial imports for a newly configured account."""
    return {
        CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
        CONF_HOURLY_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
        CONF_SCAN_PERIOD: DEFAULT_SCAN_PERIOD,
        CONF_SHOULD_IMPORT_HISTORY: True,
    }


def settings_schema(values: Mapping) -> vol.Schema:
    """Describe both setup and options with the same units and input controls."""
    days = {
        "min": 1,
        "max": MAX_HISTORY_DAYS,
        "step": 1,
        "mode": NumberSelectorMode.BOX,
        "unit_of_measurement": "days",
    }
    return vol.Schema(
        {
            vol.Required(
                CONF_HOURLY_HISTORY_DAYS, default=values[CONF_HOURLY_HISTORY_DAYS]
            ): NumberSelector(days),
            vol.Required(CONF_HISTORY_DAYS, default=values[CONF_HISTORY_DAYS]): NumberSelector(
                days
            ),
            vol.Required(
                CONF_SCAN_INTERVAL_MINUTES,
                default=values.get(CONF_SCAN_INTERVAL_MINUTES, values[CONF_SCAN_PERIOD] / 60),
            ): NumberSelector(
                {
                    "min": MIN_SCAN_PERIOD / 60,
                    "max": MAX_SCAN_PERIOD / 60,
                    "step": "any",
                    "mode": NumberSelectorMode.BOX,
                    "unit_of_measurement": "minutes",
                }
            ),
            vol.Required(
                CONF_SHOULD_IMPORT_HISTORY, default=values[CONF_SHOULD_IMPORT_HISTORY]
            ): BooleanSelector(),
        }
    )


def options_from_input(user_input: Mapping, existing_options: Mapping | None = None) -> dict:
    """Validate form input and retain unrelated existing options.

    Refresh intervals remain persisted in seconds. Decimal conversion preserves
    existing intervals that are not a whole number of minutes.
    """
    allowed = {
        CONF_HISTORY_DAYS,
        CONF_HOURLY_HISTORY_DAYS,
        CONF_SCAN_INTERVAL_MINUTES,
        CONF_SCAN_PERIOD,
        CONF_SHOULD_IMPORT_HISTORY,
    }
    if user_input.keys() - allowed:
        raise vol.Invalid("Unknown History setting")
    values = {
        **new_entry_settings(),
        CONF_SHOULD_IMPORT_HISTORY: False,
        **(existing_options or {}),
        **user_input,
    }
    values[CONF_HISTORY_DAYS] = validate_history_days(values[CONF_HISTORY_DAYS])
    values[CONF_HOURLY_HISTORY_DAYS] = validate_history_days(values[CONF_HOURLY_HISTORY_DAYS])
    if not isinstance(values[CONF_SHOULD_IMPORT_HISTORY], bool):
        raise vol.Invalid("Import after saving must be a boolean")
    if CONF_SCAN_INTERVAL_MINUTES in user_input:
        minutes = user_input[CONF_SCAN_INTERVAL_MINUTES]
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float, Decimal)):
            raise vol.Invalid("Refresh interval must be a number of minutes")
        try:
            seconds = Decimal(str(minutes)) * 60
        except InvalidOperation as error:
            raise vol.Invalid("Invalid refresh interval") from error
        # A binary float such as 3901 / 60 cannot encode the original second
        # exactly. Normalize that tiny representation error before requiring
        # whole seconds.
        if seconds.is_finite():
            nearest_second = seconds.to_integral_value()
            if abs(seconds - nearest_second) <= Decimal("0.000000001"):
                seconds = nearest_second
        values[CONF_SCAN_PERIOD] = _validate_scan_period(seconds)
    else:
        values[CONF_SCAN_PERIOD] = _validate_scan_period(values[CONF_SCAN_PERIOD])
    values.pop(CONF_SCAN_INTERVAL_MINUTES, None)
    return values
