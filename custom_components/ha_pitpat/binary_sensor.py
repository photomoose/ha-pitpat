"""Binary sensors for PitPat dogs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import unwrap
from .coordinator import PitPatConfigEntry, PitPatDogData
from .entity import PitPatEntity, async_add_dog_entities


def _lost_contact(data: PitPatDogData) -> bool | None:
    if data.monitor is None:
        return None
    # Only the wrapper's presence is known; the payload when lost is unseen.
    return bool((data.monitor.get("LostContactInfo") or {}).get("HasValue"))


@dataclass(frozen=True, kw_only=True)
class PitPatBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a PitPat dog binary sensor."""

    value_fn: Callable[[PitPatDogData], bool | None]


BINARY_SENSORS: tuple[PitPatBinarySensorDescription, ...] = (
    PitPatBinarySensorDescription(
        key="goal_met_today",
        translation_key="goal_met_today",
        value_fn=lambda data: (data.today or {}).get("HasMetExerciseGoal"),
    ),
    PitPatBinarySensorDescription(
        key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda data: (data.monitor_value("BatteryInfo") or {}).get(
            "IsCharging"
        ),
    ),
    # PitPat's own check, which the app uses for its low battery notice.
    PitPatBinarySensorDescription(
        key="needs_charging",
        translation_key="needs_charging",
        device_class=BinarySensorDeviceClass.BATTERY,
        value_fn=lambda data: data.needs_charging,
    ),
    PitPatBinarySensorDescription(
        key="lost_contact",
        translation_key="lost_contact",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_lost_contact,
    ),
    # The app marks this with "[+]" after the network type ("LTE-M [+]"). It
    # changes with the connection; probably whether the network lets PitPat
    # wake the collar straight away, rather than at its next check-in.
    PitPatBinarySensorDescription(
        key="rapid_wakeup",
        translation_key="rapid_wakeup",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: unwrap(
            (data.monitor_value("Network") or {}).get("HasRapidWakeup")
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PitPatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_dog_entities(
        entry,
        async_add_entities,
        lambda coordinator, dog_id: (
            PitPatBinarySensor(coordinator, dog_id, description)
            for description in BINARY_SENSORS
        ),
    )


class PitPatBinarySensor(PitPatEntity, BinarySensorEntity):
    """A binary sensor for one attribute of a dog."""

    entity_description: PitPatBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.dog_data)
