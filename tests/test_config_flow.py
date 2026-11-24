"""Tests for the PitPat config flow."""

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_pitpat.auth import PitPatAuthError, PitPatConnectionError
from custom_components.ha_pitpat.const import CONF_REFRESH_TOKEN, CONF_USER_ID, DOMAIN

from .conftest import USER_ID

CREDS = {CONF_EMAIL: "me@example.com", CONF_PASSWORD: "secret"}


async def test_user_flow(hass: HomeAssistant, mock_login, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "me@example.com"
    assert result["result"].unique_id == USER_ID
    assert result["data"] == {
        CONF_EMAIL: "me@example.com",
        CONF_USER_ID: USER_ID,
        CONF_REFRESH_TOKEN: "refresh-1",
    }
    assert CONF_PASSWORD not in result["data"]
    mock_login.assert_awaited_once()
    login_session, session, *creds = mock_login.await_args.args
    assert creds == ["me@example.com", "secret"]
    assert session is async_get_clientsession(hass)
    assert login_session is not session
    assert login_session.cookie_jar is not session.cookie_jar
    # Detached from Home Assistant's connector once done.
    assert login_session.closed


async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, mock_login, mock_api
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    for exc, error in (
        (PitPatAuthError("bad"), "invalid_auth"),
        (PitPatConnectionError("down"), "cannot_connect"),
    ):
        mock_login.side_effect = exc
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDS
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}

    mock_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Each submission is exactly one login attempt; no automatic retries.
    assert mock_login.await_count == 3
    # Each with a new session, cleaned up even after an error.
    sessions = [call.args[0] for call in mock_login.await_args_list]
    assert len({id(session) for session in sessions}) == 3
    assert all(session.closed for session in sessions)


async def test_already_configured(hass: HomeAssistant, mock_login, mock_api) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=USER_ID).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_login, mock_api) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={
            CONF_EMAIL: "me@example.com",
            CONF_USER_ID: USER_ID,
            CONF_REFRESH_TOKEN: "expired",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-1"


async def test_reauth_wrong_account(hass: HomeAssistant, mock_login, mock_api) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="someone-else",
        data={
            CONF_EMAIL: "me@example.com",
            CONF_USER_ID: "someone-else",
            CONF_REFRESH_TOKEN: "expired",
        },
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
