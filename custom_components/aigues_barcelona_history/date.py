"""Select the inclusive Aigües reporting dates for a manual hourly import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging

from homeassistant.components.date import DateEntity, DateEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_HISTORY_DAYS
from .service import manual_import_available
from .statistics import PROVIDER_TIME_ZONE, validate_hourly_import_range

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HistoryImportDateDescription(DateEntityDescription):
    """Map one saved date selection to its coordinator field."""

    coordinator_attribute: str


IMPORT_DATES = (
    HistoryImportDateDescription(
        key="hourly_import_start",
        name="Hourly import from",
        icon="mdi:calendar-start",
        entity_category=EntityCategory.CONFIG,
        coordinator_attribute="hourly_import_start",
    ),
    HistoryImportDateDescription(
        key="hourly_import_end",
        name="Hourly import to",
        icon="mdi:calendar-end",
        entity_category=EntityCategory.CONFIG,
        coordinator_attribute="hourly_import_end",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose date selectors only for this entry's owned contract devices."""
    async_add_entities(
        HistoryImportDate(coordinator, description)
        for coordinator in entry.runtime_data
        for description in IMPORT_DATES
    )


class HistoryImportDate(CoordinatorEntity, DateEntity, RestoreEntity):
    """Restore each date separately and validate the pair when import starts."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_extra_state_attributes = {"Date range": "Aigües reporting dates, both included"}

    def __init__(self, coordinator, description: HistoryImportDateDescription):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{coordinator.id}_{description.key}"
        self.entity_id = f"date.{self._attr_unique_id}"

    @property
    def available(self) -> bool:
        """Keep the selected dates restorable while an import is running."""
        return manual_import_available(self.coordinator, allow_during_import=True)

    @property
    def device_info(self):
        return self.coordinator.device_info

    @property
    def native_value(self) -> date:
        return getattr(self.coordinator, self.entity_description.coordinator_attribute)

    def _set_selection(self, value) -> None:
        """Use the shared provider-date validator without constraining the pair."""
        selected, _ = validate_hourly_import_range(
            value,
            value,
            today_provider=dt_util.now().astimezone(PROVIDER_TIME_ZONE).date(),
            max_days=MAX_HISTORY_DAYS,
        )
        setattr(self.coordinator, self.entity_description.coordinator_attribute, selected)

    async def async_added_to_hass(self) -> None:
        """Recover the previous selection, retaining safe defaults if invalid."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is None:
            return
        try:
            self._set_selection(last_state.state)
        except ValueError:
            _LOGGER.warning("Ignoring invalid saved hourly import date for %s", self.entity_id)

    async def async_set_value(self, value: date) -> None:
        """Save an individual date without starting a provider request."""
        try:
            self._set_selection(value)
        except ValueError as error:
            raise HomeAssistantError(str(error)) from error
        self.async_write_ha_state()
