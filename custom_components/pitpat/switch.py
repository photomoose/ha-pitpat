"""Switches for PitPat dogs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PitPatConfigEntry, PitPatCoordinator
from .entity import PendingSetting, PitPatEntity, async_add_dog_entities

BACKGROUND_TRACKING = SwitchEntityDescription(
    key="background_tracking",
    translation_key="background_tracking",
    entity_category=EntityCategory.CONFIG,
)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: PitPatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    async_add_dog_entities(
        entry,
        async_add_entities,
        lambda coordinator, dog_id: [
            PitPatBackgroundTrackingSwitch(coordinator, dog_id, BACKGROUND_TRACKING),
        ],
    )


class PitPatBackgroundTrackingSwitch(PitPatEntity, SwitchEntity):
    """Whether the monitor tracks location in the background."""

    def __init__(
        self,
        coordinator: PitPatCoordinator,
        dog_id: str,
        description: SwitchEntityDescription,
    ) -> None:
        super().__init__(coordinator, dog_id, description)
        self._pending = PendingSetting[bool]()

    @property
    def available(self) -> bool:
        return super().available and self.dog_data.monitor is not None

    @property
    def _reported(self) -> bool | None:
        value = self.dog_data.monitor_value("BackgroundTrackingEnabled")
        return None if value is None else bool(value)

    @property
    def is_on(self) -> bool | None:
        return self._pending.resolve(self._reported)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        await self._async_command(
            lambda client, user_id, dog_id: client.async_set_background_tracking(
                user_id, dog_id, enabled
            ),
            "Could not change background tracking",
        )
        self._pending.request(enabled, self._reported)
        self.async_write_ha_state()
        # Read back what the monitor now reports.
        await self.coordinator.async_refresh_monitor(self._dog_id)
