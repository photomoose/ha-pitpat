"""Base entity for PitPat dogs."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PitPatApiClient, unwrap
from .auth import PitPatAuthError, PitPatConnectionError
from .const import CONF_USER_ID, DOMAIN
from .coordinator import PitPatConfigEntry, PitPatCoordinator, PitPatDogData


@callback
def async_add_dog_entities(
    entry: PitPatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entities_for_dog: Callable[[PitPatCoordinator, str], Iterable[Entity]],
) -> None:
    """Add entities for every dog, including dogs that appear later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_dogs() -> None:
        new = set(coordinator.data) - known
        if not new:
            return
        known.update(new)
        async_add_entities(
            entity for dog_id in new for entity in entities_for_dog(coordinator, dog_id)
        )

    _add_new_dogs()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_dogs))


class PendingSetting[T]:
    """Shows a just-requested setting until the monitor reports a change.

    Monitors may keep reporting the old value until they next phone home
    (normally within about 20 minutes).
    """

    def __init__(self, timeout: timedelta = timedelta(hours=1)) -> None:
        self._timeout = timeout.total_seconds()
        # (requested value, value reported when requested, monotonic deadline)
        self._pending: tuple[T, T | None, float] | None = None

    def request(self, requested: T, reported: T | None) -> None:
        self._pending = (requested, reported, time.monotonic() + self._timeout)

    def resolve(self, reported: T | None) -> T | None:
        """Return the value to show, given what the monitor reports now."""
        if self._pending:
            requested, reported_before, deadline = self._pending
            if reported == reported_before and time.monotonic() < deadline:
                return requested
            self._pending = None
        return reported


class PitPatEntity(CoordinatorEntity[PitPatCoordinator]):
    """An entity belonging to one dog's device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PitPatCoordinator,
        dog_id: str,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._dog_id = dog_id
        self._attr_unique_id = f"{dog_id}_{description.key}"

        data = coordinator.data[dog_id]
        monitor = data.monitor or {}
        version = monitor.get("MonitorVersion") or data.dog.get("Monitor") or {}
        model = version.get("Model")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dog_id)},
            name=data.dog.get("Name"),
            manufacturer="PitPat",
            model=f"Monitor model {model}" if model is not None else None,
            serial_number=monitor.get("SerialNumber"),
            sw_version=unwrap(version.get("Firmware"))
            or version.get("FirmwareVersion"),
            hw_version=unwrap(version.get("Hardware"))
            or version.get("HardwareVersion"),
        )

    @property
    def available(self) -> bool:
        return super().available and self._dog_id in self.coordinator.data

    @property
    def dog_data(self) -> PitPatDogData:
        return self.coordinator.data[self._dog_id]

    async def _async_command(
        self,
        command: Callable[[PitPatApiClient, str, str], Awaitable[None]],
        failure: str,
    ) -> None:
        """Send a command for this dog, turning API errors into HA errors."""
        entry = self.coordinator.config_entry
        try:
            await command(
                self.coordinator.client, entry.data[CONF_USER_ID], self._dog_id
            )
        except PitPatAuthError as err:
            entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"PitPat sign-in has expired: {err}") from err
        except PitPatConnectionError as err:
            raise HomeAssistantError(f"{failure}: {err}") from err
