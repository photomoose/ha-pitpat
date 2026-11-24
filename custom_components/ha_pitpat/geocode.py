"""Reverse geocoding with OpenStreetMap's Nominatim service.

Nominatim's usage policy asks for an identifying User-Agent and at most one
request per second. Like ``api``, this module has no Home Assistant
dependencies.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .auth import TIMEOUT, PitPatConnectionError

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "HomeAssistant-PitPat/1.0.1 (+https://github.com/photomoose/ha-pitpat)"
ATTRIBUTION = "Data © OpenStreetMap contributors, ODbL"
# Seconds between requests, shared by all dogs.
MIN_INTERVAL = 1.0

_lock = asyncio.Lock()
_last_request = 0.0


@dataclass
class Place:
    """A reverse geocoded address."""

    name: str
    attributes: dict[str, Any]


async def async_reverse_geocode(
    session: aiohttp.ClientSession, latitude: float, longitude: float, language: str
) -> Place | None:
    """Return the address at a position, or None if there isn't one."""
    global _last_request
    async with _lock:
        # Capped, in case the clock has jumped since the last request.
        wait = min(_last_request + MIN_INTERVAL - time.monotonic(), MIN_INTERVAL)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with session.get(
                NOMINATIM_URL,
                params={
                    "format": "jsonv2",
                    "lat": str(latitude),
                    "lon": str(longitude),
                    "zoom": "18",
                    "addressdetails": "1",
                },
                headers={"User-Agent": USER_AGENT, "Accept-Language": language},
                timeout=TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise PitPatConnectionError(f"Reverse geocoding failed: {err}") from err
        finally:
            _last_request = time.monotonic()

    # e.g. {"error": "Unable to geocode"} out at sea.
    if not isinstance(data, dict) or "error" in data:
        return None
    address = data.get("address") or {}
    full_address = data.get("display_name")
    name = _short_address(address) or full_address
    if not name:
        return None
    return Place(
        name,
        {
            "full_address": full_address,
            "street": _street(address) or None,
            "locality": _locality(address),
            "postcode": address.get("postcode"),
            "country": address.get("country"),
        },
    )


def _street(address: dict[str, Any]) -> str:
    return " ".join(
        part for part in (address.get("house_number"), address.get("road")) if part
    )


def _locality(address: dict[str, Any]) -> str | None:
    for key in ("suburb", "village", "town", "city", "hamlet"):
        if value := address.get(key):
            return value
    return None


def _short_address(address: dict[str, Any]) -> str:
    """Street, locality and postcode, like the Companion app's sensor."""
    parts = (_street(address), _locality(address), address.get("postcode"))
    return ", ".join(part for part in parts if part)
