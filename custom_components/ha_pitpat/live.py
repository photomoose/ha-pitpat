"""Live monitor updates from PitPat's SignalR hub.

While a dog is live tracked, the app subscribes to it on a SignalR hub hosted
by Azure SignalR Service. Each time the monitor reports in (about every 30
seconds while tracking) the hub pushes its new status, in the same shape as
the Monitors endpoint. Like ``api``, this module has no Home Assistant
dependencies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

from .api import PitPatApiClient
from .auth import TIMEOUT, PitPatConnectionError
from .const import SIGNALR_USER_AGENT

_LOGGER = logging.getLogger(__name__)

# SignalR's JSON hub protocol ends every message with this character.
RECORD_SEPARATOR = "\x1e"
# Hub protocol message types.
INVOCATION = 1
COMPLETION = 3
PING = 6
CLOSE = 7
# Ping interval and server timeout, as in the SignalR clients.
PING_INTERVAL = 15
SERVER_TIMEOUT = 30

# Hub methods that push a dog's monitor.
MONITOR_TARGETS = ("subscriptionAcknowledged", "monitorChanged")

MonitorCallback = Callable[[str, dict[str, Any]], None]


class PitPatLiveConnection:
    """One connection to the hub, subscribed to any number of dogs."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client: PitPatApiClient,
        user_id: str,
        on_monitor: MonitorCallback,
    ) -> None:
        self._session = session
        self._client = client
        self._user_id = user_id
        self._on_monitor = on_monitor
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._dogs: set[str] = set()
        self._invocation_id = 0
        self._closing = False

    @property
    def dogs(self) -> set[str]:
        """The dogs currently subscribed to."""
        return set(self._dogs)

    async def async_connect(self) -> None:
        """Negotiate with PitPat, then with the hub, and open the WebSocket."""
        # PitPat redirects to the hub, with an access token for it.
        redirect = await self._client.async_negotiate_live()
        try:
            url, access_token = redirect["url"], redirect["accessToken"]
        except (KeyError, TypeError) as err:
            raise PitPatConnectionError(f"Unexpected negotiate response: {err}") from err
        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": SIGNALR_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
        }
        # Keep the query string exactly as given; it is signed into the token.
        base, _, query = url.partition("?")
        separator = "&" if query else ""
        try:
            async with asyncio.timeout(TIMEOUT.total):
                async with self._session.post(
                    URL(f"{base.rstrip('/')}/negotiate?{query}", encoded=True),
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    negotiated = await resp.json(content_type=None)
                connection_token = (
                    negotiated.get("connectionToken") or negotiated["connectionId"]
                )
                self._ws = await self._session.ws_connect(
                    URL(
                        f"{url}{separator}id={quote(connection_token, safe='')}",
                        encoded=True,
                    ),
                    headers=headers,
                )
                await self._send({"protocol": "json", "version": 1})
                handshake = json.loads(
                    (await self._ws.receive_str()).rstrip(RECORD_SEPARATOR)
                )
        except (
            aiohttp.ClientError,
            TimeoutError,
            KeyError,
            TypeError,
            ValueError,
        ) as err:
            raise PitPatConnectionError(f"Could not connect: {_describe(err)}") from err
        if handshake.get("error"):
            raise PitPatConnectionError(f"Handshake failed: {handshake['error']}")

    async def async_set_dogs(self, dog_ids: Iterable[str]) -> None:
        """Subscribe to these dogs, unsubscribing from any others."""
        wanted = set(dog_ids)
        for dog_id in self._dogs - wanted:
            await self._invoke("unsubscribeFromChanges", dog_id)
            self._dogs.discard(dog_id)
        for dog_id in wanted - self._dogs:
            await self._invoke("subscribeToChanges", dog_id)
            self._dogs.add(dog_id)

    async def async_listen(self) -> None:
        """Handle pushed messages and ping the hub.

        Returns once async_close is called; raises PitPatConnectionError if the
        connection drops.
        """
        if self._ws is None:
            raise PitPatConnectionError("Not connected")
        now = time.monotonic()
        next_ping = now + PING_INTERVAL
        last_received = now
        while True:
            now = time.monotonic()
            if now >= next_ping:
                next_ping = now + PING_INTERVAL
                await self._send({"type": PING})
            if now - last_received >= SERVER_TIMEOUT:
                raise PitPatConnectionError("Nothing received from the hub")

            deadline = min(next_ping, last_received + SERVER_TIMEOUT)
            try:
                message = await self._ws.receive(
                    timeout=max(0, deadline - time.monotonic())
                )
            except TimeoutError:
                continue
            if message.type is aiohttp.WSMsgType.TEXT:
                last_received = time.monotonic()
                self._handle(message.data)
            elif message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                if self._closing:
                    return
                raise PitPatConnectionError(f"Connection closed: {message.extra}")

    async def async_close(self) -> None:
        self._closing = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def _invoke(self, target: str, dog_id: str) -> None:
        self._invocation_id += 1
        await self._send(
            {
                "type": INVOCATION,
                "invocationId": str(self._invocation_id),
                "target": target,
                "arguments": [{"UserId": self._user_id, "DogId": dog_id}],
            }
        )

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise PitPatConnectionError("Not connected")
        try:
            await self._ws.send_str(
                json.dumps(message, separators=(",", ":")) + RECORD_SEPARATOR
            )
        except (aiohttp.ClientError, ConnectionError) as err:
            raise PitPatConnectionError(f"Send failed: {_describe(err)}") from err

    def _handle(self, data: str) -> None:
        for record in data.split(RECORD_SEPARATOR):
            if not record:
                continue
            try:
                message = json.loads(record)
            except ValueError:
                _LOGGER.debug("Ignoring unreadable hub message")
                continue
            kind = message.get("type")
            if kind == INVOCATION and message.get("target") in MONITOR_TARGETS:
                for argument in message.get("arguments") or []:
                    if not isinstance(argument, dict):
                        continue
                    dog_id = argument.get("DogId")
                    monitor = argument.get("LocationMonitor")
                    if dog_id and isinstance(monitor, dict):
                        self._on_monitor(dog_id, monitor)
            elif kind == COMPLETION and message.get("error"):
                _LOGGER.warning("Live updates request failed: %s", message["error"])
            elif kind == CLOSE:
                raise PitPatConnectionError(
                    f"Closed by the hub: {message.get('error')}"
                )


def _describe(err: BaseException) -> str:
    """Describe an error without its request details.

    The repr of a ClientResponseError (and WSServerHandshakeError) carries the
    request headers, which include the hub's access token.
    """
    if isinstance(err, aiohttp.ClientResponseError):
        return f"{type(err).__name__} {err.status} {err.message}"
    if detail := str(err):
        return f"{type(err).__name__}: {detail}"
    return type(err).__name__
