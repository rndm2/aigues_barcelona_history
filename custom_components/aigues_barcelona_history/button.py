"""Start the configured History imports from the integration's device page."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .service import manual_import_available
from .statistics import Frequency


@dataclass(frozen=True, kw_only=True)
class HistoryImportButtonDescription(ButtonEntityDescription):
    """Describe one import action without duplicating its implementation."""

    frequency: Frequency
    date_range: bool = False


IMPORT_BUTTONS = (
    HistoryImportButtonDescription(
        key="import_hourly_history",
        name="Import hourly history",
        icon="mdi:clock-arrow-down",
        entity_category=EntityCategory.CONFIG,
        frequency=Frequency.HOURLY,
    ),
    HistoryImportButtonDescription(
        key="import_daily_reference",
        name="Import daily reference",
        icon="mdi:calendar-arrow-left",
        entity_category=EntityCategory.CONFIG,
        frequency=Frequency.DAILY,
    ),
)

HOURLY_RANGE_BUTTON = HistoryImportButtonDescription(
    key="import_hourly_date_range",
    name="Import hourly date range",
    icon="mdi:calendar-clock",
    entity_category=EntityCategory.CONFIG,
    frequency=Frequency.HOURLY,
    date_range=True,
)


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose configured imports and the selected hourly reporting-date range."""
    async_add_entities(
        HistoryImportButton(coordinator, description)
        for coordinator in entry.runtime_data
        for description in (*IMPORT_BUTTONS, HOURLY_RANGE_BUTTON)
    )


class HistoryImportButton(CoordinatorEntity, ButtonEntity):
    """Run the shared entry-owned import with the current configured depth."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, description: HistoryImportButtonDescription):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{coordinator.id}_{description.key}"
        self.entity_id = f"button.{self._attr_unique_id}"

    @property
    def available(self):
        """Allow retrying an import after a failed provider update."""
        return manual_import_available(self.coordinator)

    @property
    def device_info(self):
        return self.coordinator.device_info

    async def async_press(self):
        """Use the same import path as startup and explicit service calls."""
        if self.entity_description.date_range:
            await self.coordinator.async_import_hourly_date_range()
            return
        await self.coordinator.async_import_configured_history(
            self.entity_description.frequency.value
        )
