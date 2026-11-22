"""The PitPat integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PitPatApiClient
from .auth import TokenSet
from .const import CONF_REFRESH_TOKEN
from .coordinator import PitPatConfigEntry, PitPatCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: PitPatConfigEntry) -> bool:
    """Set up PitPat from a config entry."""

    def _store_tokens(tokens: TokenSet) -> None:
        # The refresh token may rotate on every use; persist the latest one.
        if tokens.refresh_token != entry.data[CONF_REFRESH_TOKEN]:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: tokens.refresh_token}
            )

    client = PitPatApiClient.from_refresh_token(
        async_get_clientsession(hass),
        entry.data[CONF_REFRESH_TOKEN],
        _store_tokens,
    )
    coordinator = PitPatCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    _async_remove_old_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


# Entities since replaced: the Walk and Live tracking switches by buttons and
# the Tracking mode sensor, and the raw Live tracking reason by Tracking mode.
# The keep-alive switch is gone as keepAlives are now sent when needed.
REMOVED_ENTITIES = {
    Platform.SWITCH: ("_walk", "_live_tracking", "_keep_alive"),
    Platform.SENSOR: ("_live_tracking_reason",),
}


def _async_remove_old_entities(hass: HomeAssistant, entry: PitPatConfigEntry) -> None:
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        suffixes = REMOVED_ENTITIES.get(entity.domain, ())
        if entity.unique_id.endswith(suffixes):
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: PitPatConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
