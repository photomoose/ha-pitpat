"""Selects for PitPat dogs."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PitPatConfigEntry, PitPatCoordinator
from .entity import PendingSetting, PitPatEntity, async_add_dog_entities

# PhoneHomeCadence values reported by the monitor.
CADENCE_BY_VALUE = {0: "standard", 1: "economy", 2: "urgent"}

GPS_MODE = SelectEntityDescription(
    key="gps_mode",
    translation_key="gps_mode",
    entity_category=EntityCategory.CONFIG,
    options=["economy", "standard", "urgent"],
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PitPatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up selects."""
    async_add_dog_entities(
        entry,
        async_add_entities,
        lambda coordinator, dog_id: [PitPatGpsModeSelect(coordinator, dog_id, GPS_MODE)],
    )


class PitPatGpsModeSelect(PitPatEntity, SelectEntity):
    """How often the monitor reports its GPS location."""

    def __init__(
        self,
        coordinator: PitPatCoordinator,
        dog_id: str,
        description: SelectEntityDescription,
    ) -> None:
        super().__init__(coordinator, dog_id, description)
        self._pending = PendingSetting[str]()

    @property
    def available(self) -> bool:
        return super().available and self.dog_data.monitor is not None

    @property
    def _reported_option(self) -> str | None:
        return CADENCE_BY_VALUE.get(self.dog_data.monitor_value("PhoneHomeCadence"))

    @property
    def current_option(self) -> str | None:
        return self._pending.resolve(self._reported_option)

    async def async_select_option(self, option: str) -> None:
        await self._async_command(
            lambda client, user_id, dog_id: client.async_set_gps_cadence(
                user_id, dog_id, option.capitalize()
            ),
            "Could not set GPS mode",
        )
        self._pending.request(option, self._reported_option)
        self.async_write_ha_state()
        # Read back what the monitor now reports.
        await self.coordinator.async_refresh_monitor(self._dog_id)
