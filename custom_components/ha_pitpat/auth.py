"""Authentication against the PitPat IdentityServer.

The PitPat app signs in with OAuth2 authorization code + PKCE, using a custom
URL scheme as the redirect URI. Home Assistant cannot receive that redirect, so
the login form is submitted directly and the code is read from the final
``Location`` header.

This module has no Home Assistant dependencies so it can be exercised from a
plain script.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

from .const import (
    APP_USER_AGENT,
    AUTH_BASE_URL,
    AUTHORIZE_URL,
    BROWSER_USER_AGENT,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
)

MAX_REDIRECTS = 10
TIMEOUT = aiohttp.ClientTimeout(total=30)

_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w.\-:]+)\s*=\s*"([^"]*)"')


class PitPatError(Exception):
    """Base error for PitPat."""


class PitPatAuthError(PitPatError):
    """Credentials or refresh token were rejected."""


class PitPatConnectionError(PitPatError):
    """The PitPat service could not be reached or replied unexpectedly."""


@dataclass
class TokenSet:
    """Tokens returned by the token endpoint."""

    access_token: str
    refresh_token: str
    expires_at: float
    id_token: str | None = None

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> TokenSet:
        try:
            return cls(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=time.time() + int(data.get("expires_in", 3600)),
                id_token=data.get("id_token"),
            )
        except KeyError as err:
            raise PitPatConnectionError(f"Token response missing {err}") from err

    @property
    def subject(self) -> str | None:
        """Return the user id (``sub`` claim) from the ID token, if present."""
        if not self.id_token:
            return None
        try:
            payload = self.id_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        except (IndexError, ValueError):
            return None


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _form_inputs(html: str) -> dict[str, str]:
    """Return name -> value for every named <input> in the page."""
    inputs: dict[str, str] = {}
    for tag in _INPUT_TAG_RE.findall(html):
        attrs = {k.lower(): unescape(v) for k, v in _ATTR_RE.findall(tag)}
        if "name" in attrs:
            inputs[attrs["name"]] = attrs.get("value", "")
    return inputs


async def _token_request(
    session: aiohttp.ClientSession, data: dict[str, str]
) -> TokenSet:
    try:
        async with session.post(
            TOKEN_URL,
            data=data,
            headers={"Accept": "application/json", "User-Agent": APP_USER_AGENT},
            timeout=TIMEOUT,
        ) as resp:
            if resp.status in (400, 401):
                # IdentityServer returns 400 invalid_grant for bad/expired codes
                # and refresh tokens.
                body = await resp.json(content_type=None)
                raise PitPatAuthError(body.get("error", "invalid_grant"))
            resp.raise_for_status()
            return TokenSet.from_response(await resp.json(content_type=None))
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise PitPatConnectionError(f"Token request failed: {err}") from err


async def async_login(
    login_session: aiohttp.ClientSession,
    session: aiohttp.ClientSession,
    email: str,
    password: str,
) -> TokenSet:
    """Sign in with email and password and return a fresh token set.

    ``login_session`` holds the login cookies, so it must have a cookie jar of
    its own and be used for one login only. ``session`` may be shared; it
    exchanges the code for tokens. The caller owns both.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    authorize_url = f"{AUTHORIZE_URL}?" + urlencode(
        {
            "response_type": "code",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "prompt": "login",
        }
    )
    # Sent with each request: Home Assistant's sessions override a User-Agent
    # set on the session.
    browser: dict[str, Any] = {
        "headers": {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        },
        "timeout": TIMEOUT,
    }

    try:
        # /connect/authorize redirects to the login page.
        async with login_session.get(authorize_url, **browser) as resp:
            resp.raise_for_status()
            login_url = str(resp.url)
            login_html = await resp.text()

        inputs = _form_inputs(login_html)
        if "__RequestVerificationToken" not in inputs:
            raise PitPatConnectionError("Login form not found")

        async with login_session.post(
            login_url,
            data={
                "Input.Email.Value": email,
                "Input.Password.Value": password,
                "__RequestVerificationToken": inputs["__RequestVerificationToken"],
            },
            allow_redirects=False,
            **browser,
        ) as resp:
            if resp.status == 200:
                # The form is re-rendered on bad credentials or lockout.
                raise PitPatAuthError("Invalid email or password")
            if resp.status not in (301, 302, 303):
                raise PitPatConnectionError(
                    f"Unexpected login response: HTTP {resp.status}"
                )
            location = resp.headers["Location"]

        # Follow redirects until IdentityServer hands back the app redirect.
        for _ in range(MAX_REDIRECTS):
            if location.startswith(REDIRECT_URI):
                break
            async with login_session.get(
                urljoin(AUTH_BASE_URL, location),
                allow_redirects=False,
                **browser,
            ) as resp:
                if resp.status not in (301, 302, 303):
                    raise PitPatConnectionError(
                        f"Unexpected authorize response: HTTP {resp.status}"
                    )
                location = resp.headers["Location"]
        else:
            raise PitPatConnectionError("Too many redirects during login")
    except (aiohttp.ClientError, TimeoutError, KeyError) as err:
        raise PitPatConnectionError(f"Login failed: {err}") from err

    params = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
    if "error" in params:
        raise PitPatAuthError(params["error"])
    if params.get("state") != state:
        raise PitPatConnectionError("State mismatch in authorization response")
    if params.get("iss", AUTH_BASE_URL) != AUTH_BASE_URL:
        raise PitPatConnectionError("Unexpected issuer in authorization response")
    if "code" not in params:
        raise PitPatConnectionError("No authorization code returned")

    return await _token_request(
        session,
        {
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
        },
    )


async def async_refresh(session: aiohttp.ClientSession, refresh_token: str) -> TokenSet:
    """Exchange a refresh token for a new token set.

    IdentityServer may rotate refresh tokens, so callers must persist the
    returned ``refresh_token``.
    """
    return await _token_request(
        session,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )
