"""Buttons for PitPat dogs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import PitPatApiClient
from .coordinator import WAKE_UP_POLL, PitPatConfigEntry
from .entity import PitPatEntity, async_add_dog_entities


class Action(StrEnum):
    """What a tracking button does once its command is sent."""

    START_FIND = "start_find"
    START_WALK = "start_walk"
    STOP = "stop"


@dataclass(frozen=True, kw_only=True)
class PitPatButtonDescription(ButtonEntityDescription):
    """Describes a live tracking button."""

    press_fn: Callable[[PitPatApiClient, str, str], Awaitable[None]]
    failure: str
    action: Action


# The same three actions as the app.
BUTTONS: tuple[PitPatButtonDescription, ...] = (
    PitPatButtonDescription(
        key="start_live_tracking",
        translation_key="start_live_tracking",
        press_fn=lambda client, user_id, dog_id: client.async_start_find(
            user_id, dog_id
        ),
        failure="Could not start live tracking",
        action=Action.START_FIND,
    ),
    PitPatButtonDescription(
        key="start_walk",
        translation_key="start_walk",
        press_fn=lambda client, user_id, dog_id: client.async_start_walk(
            user_id, dog_id
        ),
        failure="Could not start a walk",
        action=Action.START_WALK,
    ),
    PitPatButtonDescription(
        key="stop_live_tracking",
        translation_key="stop_live_tracking",
        press_fn=lambda client, user_id, dog_id: client.async_stop_tracking(
            user_id, dog_id
        ),
        failure="Could not stop live tracking",
        action=Action.STOP,
    ),
)


WAKE_UP = ButtonEntityDescription(key="wake_up", translation_key="wake_up")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PitPatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    async_add_dog_entities(
        entry,
        async_add_entities,
        lambda coordinator, dog_id: [
            *(PitPatButton(coordinator, dog_id, description) for description in BUTTONS),
            PitPatWakeUpButton(coordinator, dog_id, WAKE_UP),
        ],
    )


class PitPatButton(PitPatEntity, ButtonEntity):
    """Starts a walk or live tracking, or stops either."""

    entity_description: PitPatButtonDescription

    async def async_press(self) -> None:
        description = self.entity_description
        await self._async_command(description.press_fn, description.failure)
        if description.action is Action.STOP:
            self.coordinator.async_live_tracking_stopped(self._dog_id)
        else:
            self.coordinator.async_expect_live_tracking()
        if description.action is Action.START_FIND:
            # Unlike a walk, this stops 10 minutes after the last keepAlive.
            self.coordinator.async_keep_find_alive(self._dog_id)
        await self.coordinator.async_refresh_monitor(self._dog_id)


class PitPatWakeUpButton(PitPatEntity, ButtonEntity):
    """Asks the monitor to report in now, rather than at its next check-in.

    PitPat wakes all the account's monitors, as the app does when it opens.
    """

    async def async_press(self) -> None:
        accepted = False

        async def wake_up(client: PitPatApiClient, user_id: str, dog_id: str) -> None:
            nonlocal accepted
            accepted = await client.async_wake_up(user_id)

        await self._async_command(wake_up, "Could not request an update")
        if not accepted:
            raise HomeAssistantError("PitPat did not accept the update request")
        self.coordinator.async_poll_quickly(WAKE_UP_POLL)
        # Also starts the quicker polling schedule.
        await self.coordinator.async_refresh_monitor(self._dog_id)
