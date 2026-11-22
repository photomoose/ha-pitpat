"""Config flow for PitPat."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import PitPatApiClient
from .auth import PitPatAuthError, PitPatConnectionError, async_login
from .const import CONF_REFRESH_TOKEN, CONF_USER_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})


class PitPatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for PitPat."""

    VERSION = 1

    async def _async_authenticate(
        self, email: str, password: str
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Log in; return (entry data, errors). The password is never stored."""
        try:
            tokens = await async_login(email, password)
            client = PitPatApiClient(async_get_clientsession(self.hass), tokens)
            settings = await client.async_get_settings()
        except PitPatAuthError:
            return None, {"base": "invalid_auth"}
        except PitPatConnectionError:
            _LOGGER.exception("Error connecting to PitPat")
            return None, {"base": "cannot_connect"}
        return {
            CONF_EMAIL: email,
            CONF_USER_ID: settings["UserId"],
            CONF_REFRESH_TOKEN: client.tokens.refresh_token,
        }, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._async_authenticate(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if data:
                await self.async_set_unique_id(data[CONF_USER_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=data[CONF_EMAIL], data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {CONF_EMAIL: (user_input or {}).get(CONF_EMAIL)}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._async_authenticate(
                entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if data:
                await self.async_set_unique_id(data[CONF_USER_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
            errors=errors,
        )
