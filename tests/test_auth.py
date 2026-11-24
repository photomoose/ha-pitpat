"""Tests for signing in, against a local fake of the PitPat IdentityServer."""

from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from custom_components.ha_pitpat import auth
from custom_components.ha_pitpat.const import (
    APP_USER_AGENT,
    BROWSER_USER_AGENT,
    REDIRECT_URI,
)

LOGIN_PAGE = '<input name="__RequestVerificationToken" type="hidden" value="csrf" />'


class FakeIdentityServer:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/connect/authorize", self.authorize)
        self.app.router.add_get("/login", self.login_page)
        self.app.router.add_post("/login", self.login)
        self.app.router.add_get("/callback", self.callback)
        self.app.router.add_post("/connect/token", self.token)
        self.state = ""
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def _record(self, request: web.Request) -> None:
        self.requests.append(
            (request.path, request.headers["User-Agent"], dict(request.cookies))
        )

    async def authorize(self, request: web.Request) -> web.Response:
        self._record(request)
        self.state = request.query["state"]
        return web.Response(status=302, headers={"Location": "/login"})

    async def login_page(self, request: web.Request) -> web.Response:
        self._record(request)
        response = web.Response(text=LOGIN_PAGE, content_type="text/html")
        response.set_cookie("antiforgery", "1")
        return response

    async def login(self, request: web.Request) -> web.Response:
        self._record(request)
        form = await request.post()
        assert form["__RequestVerificationToken"] == "csrf"
        if form["Input.Password.Value"] != "secret":
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        response = web.Response(status=302, headers={"Location": "/callback"})
        response.set_cookie("signed_in", "1")
        return response

    async def callback(self, request: web.Request) -> web.Response:
        self._record(request)
        location = f"{REDIRECT_URI}?code=the-code&state={self.state}"
        return web.Response(status=302, headers={"Location": location})

    async def token(self, request: web.Request) -> web.Response:
        self._record(request)
        assert (await request.post())["code"] == "the-code"
        return web.json_response(
            {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
        )


@pytest.fixture
async def server(socket_enabled):
    fake = FakeIdentityServer()
    server = TestServer(fake.app, host="127.0.0.1")
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    with (
        patch.object(auth, "AUTH_BASE_URL", base),
        patch.object(auth, "AUTHORIZE_URL", f"{base}/connect/authorize"),
        patch.object(auth, "TOKEN_URL", f"{base}/connect/token"),
    ):
        yield fake
    await server.close()


@pytest.fixture
async def login_session(hass: HomeAssistant):
    # unsafe, as cookies are otherwise not kept for IP addresses.
    session = async_create_clientsession(
        hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar(unsafe=True)
    )
    yield session
    session.detach()


async def test_login(
    hass: HomeAssistant, server: FakeIdentityServer, login_session
) -> None:
    session = async_get_clientsession(hass)
    tokens = await auth.async_login(login_session, session, "me@example.com", "secret")

    assert tokens.refresh_token == "refresh"
    antiforgery = {"antiforgery": "1"}
    assert server.requests == [
        ("/connect/authorize", BROWSER_USER_AGENT, {}),
        # Also sent when a redirect is followed.
        ("/login", BROWSER_USER_AGENT, {}),
        ("/login", BROWSER_USER_AGENT, antiforgery),
        ("/callback", BROWSER_USER_AGENT, {**antiforgery, "signed_in": "1"}),
        # No login cookies in the shared session.
        ("/connect/token", APP_USER_AGENT, {}),
    ]
    assert not list(session.cookie_jar)
    # Left open for the caller to clean up.
    assert not login_session.closed


async def test_login_wrong_password(
    hass: HomeAssistant, server: FakeIdentityServer, login_session
) -> None:
    with pytest.raises(auth.PitPatAuthError):
        await auth.async_login(
            login_session, async_get_clientsession(hass), "me@example.com", "wrong"
        )
    assert server.requests[-1][0] == "/login"


async def test_login_creates_no_sessions(
    hass: HomeAssistant, server: FakeIdentityServer, login_session
) -> None:
    """A session of its own would load the CA bundle on the event loop."""
    session = async_get_clientsession(hass)
    with patch.object(
        aiohttp, "ClientSession", side_effect=AssertionError("own session")
    ):
        await auth.async_login(login_session, session, "me@example.com", "secret")
