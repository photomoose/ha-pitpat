"""Client for the PitPat REST API.

Like ``auth``, this module has no Home Assistant dependencies.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

import aiohttp

from .auth import (
    TIMEOUT,
    PitPatAuthError,
    PitPatConnectionError,
    TokenSet,
    async_refresh,
)
from .const import (
    ACTIVITY_BASE_URL,
    API_BASE_URL,
    API_USER_AGENT,
    LOCATION_BASE_URL,
)

# Refresh this long before the access token actually expires.
TOKEN_REFRESH_MARGIN = 60

TokenCallback = Callable[[TokenSet], Awaitable[None] | None]


class PitPatApiClient:
    """Authenticated access to api.pitpat.com."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenSet,
        on_tokens_updated: TokenCallback | None = None,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._on_tokens_updated = on_tokens_updated
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def from_refresh_token(
        cls,
        session: aiohttp.ClientSession,
        refresh_token: str,
        on_tokens_updated: TokenCallback | None = None,
    ) -> PitPatApiClient:
        """Create a client that fetches an access token on first use."""
        return cls(session, TokenSet("", refresh_token, 0), on_tokens_updated)

    @property
    def tokens(self) -> TokenSet:
        return self._tokens

    async def _refresh(self, stale_access_token: str) -> None:
        async with self._refresh_lock:
            # Another caller may have refreshed while we waited for the lock.
            if self._tokens.access_token != stale_access_token:
                return
            self._tokens = await async_refresh(
                self._session, self._tokens.refresh_token
            )
            if self._on_tokens_updated:
                result = self._on_tokens_updated(self._tokens)
                if isinstance(result, Awaitable):
                    await result

    async def _request(
        self, method: str, path: str, base_url: str = API_BASE_URL, **kwargs: Any
    ) -> Any:
        if self._tokens.expires_at - TOKEN_REFRESH_MARGIN < time.time():
            await self._refresh(self._tokens.access_token)

        for attempt in range(2):
            access_token = self._tokens.access_token
            try:
                async with self._session.request(
                    method,
                    f"{base_url}{path}",
                    headers={
                        "Accept": "*/*",
                        "Accept-Language": "en-GB",
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": API_USER_AGENT,
                    },
                    timeout=TIMEOUT,
                    **kwargs,
                ) as resp:
                    if resp.status == 401:
                        if attempt == 0:
                            await self._refresh(access_token)
                            continue
                        raise PitPatAuthError("Access token rejected")
                    resp.raise_for_status()
                    # Commands reply 200 with an empty body.
                    body = await resp.text()
                    return json.loads(body) if body else None
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise PitPatConnectionError(f"{method} {path} failed: {err}") from err
        raise PitPatAuthError("Access token rejected")

    async def async_get_settings(self) -> dict[str, Any]:
        """Return account settings, including ``UserId`` and ``UserDogTotal``."""
        return await self._request("GET", "/api/Settings")

    async def async_get_dogs(self, user_id: str) -> list[dict[str, Any]]:
        """Return the user's dogs, each with its ``Breed`` and ``Monitor``."""
        return await self._request("GET", f"/api/Users/{user_id}/Dogs")

    async def async_get_monitor(
        self, user_id: str, dog_id: str
    ) -> dict[str, Any] | None:
        """Return live status for the dog's monitor, or None if it has none."""
        data = unwrap(
            await self._request("GET", f"/api/Users/{user_id}/Dogs/{dog_id}/Monitors")
        )
        return (data or {}).get("Monitor")

    async def async_get_activity(
        self, user_id: str, dog_id: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Return daily activity, grouped by month, for the dates given.

        The service returns every day of each month the range touches, newest
        first, each with ``ExerciseMinutes``, ``PotteringMinutes``,
        ``RestingMinutes`` and ``ExerciseGoal``.
        """
        return await self._request(
            "GET",
            f"/api/Users/{user_id}/Dogs/{dog_id}/ActivityMonthAndDaysByMonth",
            base_url=ACTIVITY_BASE_URL,
            params={
                "startDate": f"{start.isoformat()}T00:00:00.0000000Z",
                "endDate": f"{end.isoformat()}T00:00:00.0000000Z",
                "locale": "en-GB",
            },
        )

    async def async_get_combined_activity(
        self, user_id: str, dog_id: str, day: date
    ) -> dict[str, Any]:
        """Return detailed activity for one day.

        ``ActivityDay`` holds the day's totals (steps, distance in metres,
        calories, walk/run/play minutes). ``StepBlocksForToday`` and
        ``ActivityBlocks`` hold the 5-minute breakdown.
        """
        return await self._request(
            "GET",
            f"/api/Users/{user_id}/Dogs/{dog_id}/CombinedActivity/"
            f"{day.isoformat()}T00:00:00.0000000Z",
            base_url=ACTIVITY_BASE_URL,
        )

    async def async_get_total_distance(
        self, user_id: str, dog_id: str
    ) -> dict[str, Any]:
        """Return the dog's lifetime ``DistanceInMeters`` and ``HasActivityData``."""
        return await self._request(
            "GET",
            f"/api/Users/{user_id}/Dogs/{dog_id}/GetTotalDistance",
            base_url=ACTIVITY_BASE_URL,
        )

    async def async_start_walk(self, user_id: str, dog_id: str) -> None:
        """Start a walk, which switches the monitor to live GPS tracking."""
        await self._request(
            "PUT",
            f"/api/2/user/{user_id}/dog/{dog_id}/livetracking/start/walk",
            base_url=LOCATION_BASE_URL,
        )

    async def async_start_find(self, user_id: str, dog_id: str) -> None:
        """Start live tracking, to find the dog."""
        # Unlike start/walk, the app calls this without the "/2" version segment.
        await self._request(
            "PUT",
            f"/api/user/{user_id}/dog/{dog_id}/livetracking/start/find",
            base_url=LOCATION_BASE_URL,
        )

    async def async_set_gps_cadence(
        self, user_id: str, dog_id: str, cadence: str
    ) -> None:
        """Set how often the monitor reports its location.

        ``cadence`` is ``Economy``, ``Standard`` or ``Urgent``.
        """
        await self._request(
            "PUT",
            f"/api/user/{user_id}/dog/{dog_id}/monitor/updatePermanentCadence",
            base_url=LOCATION_BASE_URL,
            params={"cadence": cadence},
        )

    async def async_set_background_tracking(
        self, user_id: str, dog_id: str, enabled: bool
    ) -> None:
        """Turn the monitor's background location tracking on or off."""
        await self._request(
            "PUT",
            f"/api/user/{user_id}/dog/{dog_id}/monitor/updateBackgroundTracking",
            base_url=LOCATION_BASE_URL,
            params={"isEnabled": "True" if enabled else "False"},
        )

    async def async_negotiate_live(self) -> dict[str, Any]:
        """Return the SignalR service ``url`` and ``accessToken`` for live updates."""
        return await self._request(
            "POST",
            "/PitPatLocationHub/negotiate",
            base_url=LOCATION_BASE_URL,
            params={"negotiateVersion": "1"},
        )

    async def async_keep_alive(self, user_id: str, dog_id: str) -> None:
        """Keep live tracking to find the dog going.

        The monitor stops tracking 10 minutes after the last keepAlive (or
        after it started). The app sends this every 30 seconds while its map is
        open; walks don't need it.
        """
        await self._request(
            "PUT",
            f"/api/2/user/{user_id}/dog/{dog_id}/monitor/keepAlive",
            base_url=LOCATION_BASE_URL,
        )

    async def async_wake_up(self, user_id: str) -> bool:
        """Ask all the user's monitors to contact PitPat now.

        The app sends this when it opens and on each change of tab. Returns
        whether PitPat accepted the request.
        """
        return bool(
            await self._request(
                "PUT",
                f"/api/user/{user_id}/monitors/wakeup",
                base_url=LOCATION_BASE_URL,
                params={"source": "Other"},
            )
        )

    async def async_needs_charging(self, user_id: str, dog_id: str) -> bool:
        """Return whether PitPat says the monitor needs charging."""
        return bool(
            await self._request(
                "GET",
                f"/api/user/{user_id}/dog/{dog_id}/monitor/batterycheck",
                base_url=LOCATION_BASE_URL,
            )
        )

    async def async_get_walks(
        self, user_id: str, dog_id: str, day: date
    ) -> list[dict[str, Any]]:
        """Return the walks (recorded tracking sessions) on a day.

        Each has ``Start``, ``End`` and an optional ``Summary`` with the
        ``Vicinity`` and, once processed, an ``Activity`` summary.
        """
        result = await self._request(
            "GET",
            f"/api/user/{user_id}/dog/{dog_id}/tracking/day/recorded",
            base_url=LOCATION_BASE_URL,
            params={"date": day.isoformat()},
        )
        return list((result or {}).get("Sessions") or [])

    async def async_stop_tracking(self, user_id: str, dog_id: str) -> None:
        """Stop live tracking, whether started as a walk or to find the dog."""
        await self._request(
            "PUT",
            f"/api/user/{user_id}/dog/{dog_id}/livetracking/stop",
            base_url=LOCATION_BASE_URL,
        )


def unwrap(value: Any) -> Any:
    """Unwrap the API's ``{"HasValue": bool, "Value": ...}`` optional wrapper."""
    if isinstance(value, dict) and "HasValue" in value:
        return value.get("Value") if value["HasValue"] else None
    return value


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an API timestamp; ``0001-01-01`` means "never" and maps to None."""
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
