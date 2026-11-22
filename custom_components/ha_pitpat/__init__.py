"""The PitPat integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
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

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PitPatConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
