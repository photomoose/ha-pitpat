"""GPS location of PitPat dogs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PitPatConfigEntry
from .entity import PitPatEntity, async_add_dog_entities

LOCATION = EntityDescription(key="location", translation_key="location")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PitPatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the dog location tracker."""
    async_add_dog_entities(
        entry,
        async_add_entities,
        lambda coordinator, dog_id: [PitPatTracker(coordinator, dog_id, LOCATION)],
    )


class PitPatTracker(PitPatEntity, TrackerEntity):
    """The dog's last known GPS position."""

    _attr_name = None
    _attr_source_type = SourceType.GPS

    @property
    def _position(self) -> dict[str, Any]:
        return self.dog_data.monitor_value("LastKnownPosition") or {}

    @property
    def latitude(self) -> float | None:
        return self._position.get("Latitude")

    @property
    def longitude(self) -> float | None:
        return self._position.get("Longitude")

    @property
    def location_accuracy(self) -> float:
        return (self._position.get("Accuracy") or {}).get("Metres") or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"position_time": self._position.get("DataTime")}
