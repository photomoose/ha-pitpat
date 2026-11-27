"""Tests for the SignalR live updates client."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.ha_pitpat import live
from custom_components.ha_pitpat.auth import PitPatConnectionError
from custom_components.ha_pitpat.live import RECORD_SEPARATOR, PitPatLiveConnection

from .conftest import DOG_ID, MONITOR_RESPONSE, USER_ID

HUB_QUERY = "hub=pitpatlocationhub&asrs.op=%2FPitPatLocationHub&asrs_request_id=abc%3D"
MONITOR = MONITOR_RESPONSE["Value"]["Monitor"]

# The fake hub is a real local server.
pytestmark = pytest.mark.usefixtures("socket_enabled")


def _encode(message: dict) -> str:
    return json.dumps(message) + RECORD_SEPARATOR


class FakeHub:
    """Answers like Azure SignalR Service for the location hub."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        self.received: list[dict] = []
        self.ws: web.WebSocketResponse | None = None
        self.subscribed = asyncio.Event()
        self.reject_negotiate = False
        self.app = web.Application()
        self.app.router.add_post("/client/negotiate", self.negotiate)
        self.app.router.add_get("/client/", self.websocket)

    async def negotiate(self, request: web.Request) -> web.Response:
        self.requests.append(
            ("negotiate", request.rel_url.raw_query_string, request.headers.get("Authorization"))
        )
        if self.reject_negotiate:
            return web.Response(status=401, text="Unauthorized")
        return web.json_response(
            {"negotiateVersion": 1, "connectionId": "cid", "connectionToken": "c/t"}
        )

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        self.requests.append(
            ("websocket", request.rel_url.raw_query_string, request.headers.get("Authorization"))
        )
        ws = self.ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            for record in message.data.split(RECORD_SEPARATOR):
                if not record:
                    continue
                data = json.loads(record)
                self.received.append(data)
                if "protocol" in data:
                    await ws.send_str("{}" + RECORD_SEPARATOR)
                elif data.get("target") == "subscribeToChanges":
                    await ws.send_str(
                        _encode(
                            {
                                "type": 1,
                                "target": "subscriptionAcknowledged",
                                "arguments": [
                                    {"DogId": DOG_ID, "LocationMonitor": MONITOR}
                                ],
                            }
                        )
                        + _encode(
                            {
                                "type": 3,
                                "invocationId": data["invocationId"],
                                "result": None,
                            }
                        )
                    )
                    self.subscribed.set()
        return ws


@pytest.fixture
async def hub():
    fake = FakeHub()
    server = TestServer(fake.app, host="127.0.0.1")
    await server.start_server()
    fake.url = str(server.make_url("/client/")) + "?" + HUB_QUERY
    yield fake
    await server.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as session:
        yield session


def _client(hub: FakeHub) -> AsyncMock:
    client = AsyncMock()
    client.async_negotiate_live.return_value = {
        "negotiateVersion": 0,
        "url": hub.url,
        "accessToken": "hub-token",
        "availableTransports": [],
    }
    return client


async def test_receives_pushed_monitors(hub: FakeHub, session) -> None:
    client = _client(hub)
    pushed: list[tuple[str, dict]] = []
    connection = PitPatLiveConnection(
        session, client, USER_ID, lambda dog_id, monitor: pushed.append((dog_id, monitor))
    )
    await connection.async_connect()

    # Both negotiations use the hub's token and keep the signed query as given.
    assert hub.requests == [
        ("negotiate", HUB_QUERY, "Bearer hub-token"),
        ("websocket", f"{HUB_QUERY}&id=c%2Ft", "Bearer hub-token"),
    ]

    await connection.async_set_dogs([DOG_ID])
    listener = asyncio.create_task(connection.async_listen())
    await asyncio.wait_for(hub.subscribed.wait(), 5)

    moved = {**MONITOR, "LiveTrackingReason": 2}
    await hub.ws.send_str(
        _encode(
            {
                "type": 1,
                "target": "monitorChanged",
                "arguments": [{"DogId": DOG_ID, "LocationMonitor": moved}],
            }
        )
        + _encode({"type": 6})
    )
    async with asyncio.timeout(5):
        while len(pushed) < 2:
            await asyncio.sleep(0.01)
    assert pushed == [(DOG_ID, MONITOR), (DOG_ID, moved)]

    await connection.async_set_dogs([])
    await connection.async_close()
    await asyncio.wait_for(listener, 5)

    assert hub.received[0] == {"protocol": "json", "version": 1}
    assert [m.get("target") for m in hub.received[1:]] == [
        "subscribeToChanges",
        "unsubscribeFromChanges",
    ]
    assert hub.received[1] == {
        "type": 1,
        "invocationId": "1",
        "target": "subscribeToChanges",
        "arguments": [{"UserId": USER_ID, "DogId": DOG_ID}],
    }
    assert hub.received[2]["invocationId"] == "2"


async def test_pings_hub(hub: FakeHub, session) -> None:
    connection = PitPatLiveConnection(session, _client(hub), USER_ID, lambda *_: None)
    await connection.async_connect()
    with patch.object(live, "PING_INTERVAL", 0.05):
        listener = asyncio.create_task(connection.async_listen())
        async with asyncio.timeout(5):
            while {"type": 6} not in hub.received:
                await asyncio.sleep(0.01)
    await connection.async_close()
    await asyncio.wait_for(listener, 5)


async def test_dropped_connection_raises(hub: FakeHub, session) -> None:
    connection = PitPatLiveConnection(session, _client(hub), USER_ID, lambda *_: None)
    await connection.async_connect()
    listener = asyncio.create_task(connection.async_listen())
    await asyncio.sleep(0.05)
    await hub.ws.close()
    with pytest.raises(PitPatConnectionError):
        await asyncio.wait_for(listener, 5)
    await connection.async_close()


async def test_hub_close_message_raises(hub: FakeHub, session) -> None:
    connection = PitPatLiveConnection(session, _client(hub), USER_ID, lambda *_: None)
    await connection.async_connect()
    listener = asyncio.create_task(connection.async_listen())
    await asyncio.sleep(0.05)
    await hub.ws.send_str(_encode({"type": 7, "error": "Server shutting down"}))
    with pytest.raises(PitPatConnectionError, match="Server shutting down"):
        await asyncio.wait_for(listener, 5)
    await connection.async_close()


async def test_bad_negotiate_response(session) -> None:
    client = AsyncMock()
    client.async_negotiate_live.return_value = {"error": "nope"}
    connection = PitPatLiveConnection(session, client, USER_ID, lambda *_: None)
    with pytest.raises(PitPatConnectionError, match="negotiate"):
        await connection.async_connect()


async def test_rejected_negotiate_keeps_token_out_of_error(hub: FakeHub, session) -> None:
    hub.reject_negotiate = True
    connection = PitPatLiveConnection(session, _client(hub), USER_ID, lambda *_: None)
    with pytest.raises(PitPatConnectionError) as exc_info:
        await connection.async_connect()
    assert str(exc_info.value) == "Could not connect: ClientResponseError 401 Unauthorized"
    assert "hub-token" not in str(exc_info.value)
