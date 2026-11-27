"""Data update coordinator for PitPat."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import PitPatApiClient, parse_datetime, unwrap
from .auth import PitPatAuthError, PitPatConnectionError
from .const import CONF_USER_ID, DOMAIN
from .live import PitPatLiveConnection

_LOGGER = logging.getLogger(__name__)

# Collars report in roughly every 20 minutes (less often in Economy mode).
# Polling the monitors this often catches each report soon after it arrives;
# the dogs and their activity are only fetched again once one has, as only a
# report brings new activity.
UPDATE_INTERVAL = timedelta(minutes=5)
# Fetch everything at least this often anyway, in case something changed
# without a report: activity synced by the app over Bluetooth, or the dog's
# details edited in the app.
FULL_UPDATE_INTERVAL = timedelta(hours=1)
# While a dog is being live tracked (e.g. on a walk), poll its monitor quickly
# so the map follows along. Reports then come every few seconds, so dogs and
# activity refresh every UPDATE_INTERVAL instead of on each one.
# PitPat ends a walk by itself after three hours, and live tracking to find the
# dog 10 minutes after the last keepAlive; LiveTrackingReason then returns to 0
# and polling slows down again.
LIVE_UPDATE_INTERVAL = timedelta(seconds=30)
# While connected to live updates the hub pushes every report (including the
# end of tracking), so polling is only a fallback.
LIVE_CONNECTED_UPDATE_INTERVAL = timedelta(minutes=2)
# The monitor has to wake up before tracking begins, which the app says can
# take a few minutes. Poll quickly for this long after tracking is requested.
LIVE_START_GRACE = timedelta(minutes=10)
# Detailed activity (CombinedActivity) is a large response that only changes
# when the collar reports. It is fetched after each report, but reused for
# this long by the five-minute full updates during a walk or live tracking.
# Today's walks are reused the same way, except during a walk itself, so a
# finished walk shows up soon after it ends.
DETAILED_ACTIVITY_INTERVAL = timedelta(minutes=15)
# Live tracking to find the dog stops 10 minutes after the last keepAlive. The
# app sends one every 30 seconds while its map is open; we send them after
# tracking is started from Home Assistant, for at most as long as a walk lasts.
KEEP_ALIVE_INTERVAL = timedelta(seconds=30)
KEEP_ALIVE_LIMIT = timedelta(hours=3)
# After asking the monitors to wake up, poll quickly for at most this long,
# until each reports in.
WAKE_UP_POLL = timedelta(minutes=3)
# Poll this long after a monitor is next expected to report, to pick the report
# up straight away rather than at the next regular poll; then once more, 30
# seconds later, if it hasn't reported by then.
CONTACT_POLL_DELAYS = (timedelta(seconds=5), timedelta(seconds=35))
# A report the hub hasn't pushed this long after it was sent means the
# subscription has silently stopped working.
LIVE_PUSH_TIMEOUT = timedelta(seconds=30)

# Values of the monitor's LiveTrackingReason.
LIVE_TRACKING_NONE = 0
LIVE_TRACKING_FIND = 1
LIVE_TRACKING_WALK = 2

type PitPatConfigEntry = ConfigEntry[PitPatCoordinator]


@dataclass
class PitPatDogData:
    """Everything known about one dog."""

    dog: dict[str, Any]
    monitor: dict[str, Any] | None
    # The dog's time zone, or None to use Home Assistant's.
    time_zone: tzinfo | None = None
    # Activity for the dog's local today/yesterday and the current month.
    today: dict[str, Any] | None = None
    yesterday: dict[str, Any] | None = None
    month: dict[str, Any] | None = None
    # Today's detailed totals (CombinedActivity's ActivityDay), the local day
    # they are for, and the lifetime step count and distance (in metres).
    today_detail: dict[str, Any] | None = None
    today_detail_date: date | None = None
    total_steps: int | None = None
    total_distance: float | None = None
    # PitPat's own view of whether the monitor needs charging.
    needs_charging: bool | None = None
    # Today's walks, oldest first, and the local day they are for.
    walks: list[dict[str, Any]] | None = None
    walks_date: date | None = None

    def monitor_value(self, key: str) -> Any:
        """Return an unwrapped top-level monitor field, or None."""
        return unwrap((self.monitor or {}).get(key))

    @property
    def live_tracking(self) -> bool:
        return (self.monitor_value("LiveTrackingReason") or LIVE_TRACKING_NONE) != (
            LIVE_TRACKING_NONE
        )

    @property
    def last_message_sent_at(self) -> datetime | None:
        timings = self.monitor_value("ContactTimings") or {}
        return parse_datetime(timings.get("LastMessageSentAt"))

    @property
    def next_message_expected_at(self) -> datetime | None:
        timings = self.monitor_value("ContactTimings") or {}
        return parse_datetime(timings.get("NextMessageExpectedAt"))


class PitPatCoordinator(DataUpdateCoordinator[dict[str, PitPatDogData]]):
    """Fetches all dogs with their monitor and activity; keyed by dog id."""

    config_entry: PitPatConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: PitPatConfigEntry, client: PitPatApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self._last_full_update = 0.0
        self._fast_poll_until = 0.0
        # Monotonic time of each dog's last successful detailed activity and
        # walks fetches.
        self._detail_fetched_at: dict[str, float] = {}
        self._walks_fetched_at: dict[str, float] = {}
        # Pushed monitor updates while any dog is live tracked.
        self._live_task: asyncio.Task[None] | None = None
        self._live_connection: PitPatLiveConnection | None = None
        self._unsub_live_reconnect: CALLBACK_TYPE | None = None
        # Dogs whose live tracking to find them we keep going: until when
        # (monotonic), and those it has been seen running for.
        self._keep_alive_until: dict[str, float] = {}
        self._keep_alive_seen: set[str] = set()
        self._unsub_keep_alive: CALLBACK_TYPE | None = None
        # When each dog last reported, and its local day, as of its last full
        # update.
        self._full_update_reports: dict[str, datetime | None] = {}
        self._full_update_days: dict[str, date] = {}
        # Dogs asked to report in, with when they last reported before that,
        # and until when (monotonic) to poll quickly for their reports.
        self._wake_up_reported_before: dict[str, datetime | None] = {}
        self._wake_up_until = 0.0
        # A one-off poll just after the next report is expected.
        self._contact_poll_at: datetime | None = None
        self._unsub_contact_poll: CALLBACK_TYPE | None = None

    @callback
    def async_expect_live_tracking(self) -> None:
        """Poll quickly for a while; called after live tracking is started."""
        self.async_poll_quickly(LIVE_START_GRACE)

    @callback
    def async_poll_quickly(self, period: timedelta) -> None:
        """Poll monitors quickly for a while, e.g. while one is waking up."""
        self._fast_poll_until = max(
            self._fast_poll_until, time.monotonic() + period.total_seconds()
        )
        self.update_interval = LIVE_UPDATE_INTERVAL

    @callback
    def async_expect_reports(self) -> None:
        """Poll quickly until each dog reports in; called after a wake-up."""
        self._wake_up_reported_before = {
            dog_id: dog_data.last_message_sent_at
            for dog_id, dog_data in (self.data or {}).items()
        }
        self._wake_up_until = time.monotonic() + WAKE_UP_POLL.total_seconds()
        self.update_interval = LIVE_UPDATE_INTERVAL

    def _async_new_reports(self, polled: dict[str, PitPatDogData]) -> set[str]:
        """Return the dogs that have reported in since their last full update.

        Dogs being live tracked are left out: they report every few seconds.
        """
        return {
            dog_id
            for dog_id, dog_data in polled.items()
            if dog_id in self._full_update_reports
            and not dog_data.live_tracking
            and _is_newer(
                dog_data.last_message_sent_at, self._full_update_reports[dog_id]
            )
        }

    @callback
    def _async_check_woken_up(self, data: dict[str, PitPatDogData]) -> None:
        """Stop waiting for the dogs that have reported in since a wake-up."""
        if time.monotonic() >= self._wake_up_until:
            self._wake_up_reported_before.clear()
            return
        for dog_id, before in list(self._wake_up_reported_before.items()):
            if dog_id not in data or _is_newer(
                data[dog_id].last_message_sent_at, before
            ):
                del self._wake_up_reported_before[dog_id]

    @callback
    def async_live_tracking_stopped(self, dog_id: str) -> None:
        """Stop fast polling and keepAlives; called after tracking is stopped."""
        self._fast_poll_until = 0.0
        self._async_stop_keep_alive(dog_id)

    @callback
    def async_keep_find_alive(self, dog_id: str) -> None:
        """Keep live tracking to find the dog going; called after starting it."""
        self._keep_alive_until[dog_id] = (
            time.monotonic() + KEEP_ALIVE_LIMIT.total_seconds()
        )
        self._keep_alive_seen.discard(dog_id)
        if self._unsub_keep_alive is None:
            self._unsub_keep_alive = async_track_time_interval(
                self.hass,
                self._async_send_keep_alives,
                KEEP_ALIVE_INTERVAL,
                name="pitpat keepAlive",
                cancel_on_shutdown=True,
            )

    @callback
    def _async_stop_keep_alive(self, dog_id: str) -> None:
        self._keep_alive_until.pop(dog_id, None)
        self._keep_alive_seen.discard(dog_id)
        if not self._keep_alive_until and self._unsub_keep_alive is not None:
            self._unsub_keep_alive()
            self._unsub_keep_alive = None

    @callback
    def _async_send_keep_alives(self, _now: datetime) -> None:
        now = time.monotonic()
        for dog_id, until in list(self._keep_alive_until.items()):
            dog_data = (self.data or {}).get(dog_id)
            reason = dog_data.monitor_value("LiveTrackingReason") if dog_data else None
            if reason == LIVE_TRACKING_FIND:
                self._keep_alive_seen.add(dog_id)
            # Stop once tracking has ended (or become a walk), or at the limit.
            if (
                dog_data is None
                or now >= until
                or (dog_id in self._keep_alive_seen and reason != LIVE_TRACKING_FIND)
            ):
                _LOGGER.debug("No longer keeping live tracking going for %s", dog_id)
                self._async_stop_keep_alive(dog_id)
                continue
            self.config_entry.async_create_background_task(
                self.hass, self._async_keep_alive(dog_id), "pitpat keepAlive"
            )

    async def _async_keep_alive(self, dog_id: str) -> None:
        try:
            await self.client.async_keep_alive(
                self.config_entry.data[CONF_USER_ID], dog_id
            )
        except PitPatAuthError:
            self.config_entry.async_start_reauth(self.hass)
        except PitPatConnectionError as err:
            _LOGGER.debug("keepAlive for %s failed: %s", dog_id, err)
        else:
            _LOGGER.debug("Sent keepAlive for %s", dog_id)

    @callback
    def async_update_listeners(self) -> None:
        self._async_update_live_connection()
        self._async_schedule_contact_poll()
        super().async_update_listeners()

    @callback
    def _async_schedule_contact_poll(self) -> None:
        """Poll just after the next report is expected, and again if it's late.

        On top of the regular polling, which carries on as before. Dogs being
        live tracked are left out: they report every few seconds. Once a dog
        reports, its expected time moves on, and with it the polls. A collar
        that misses both is left to the regular polling.
        """
        now = dt_util.utcnow()
        due = [
            when
            for dog_data in (self.data or {}).values()
            if not dog_data.live_tracking
            and (expected := dog_data.next_message_expected_at) is not None
            for delay in CONTACT_POLL_DELAYS
            if (when := expected + delay) > now
        ]
        when = min(due, default=None)
        if when == self._contact_poll_at:
            return
        self._async_cancel_contact_poll()
        if when is not None:
            self._contact_poll_at = when
            self._unsub_contact_poll = async_track_point_in_utc_time(
                self.hass, self._async_contact_poll, when
            )

    @callback
    def _async_cancel_contact_poll(self) -> None:
        if self._unsub_contact_poll is not None:
            self._unsub_contact_poll()
            self._unsub_contact_poll = None
        self._contact_poll_at = None

    async def _async_contact_poll(self, _now: datetime) -> None:
        self._unsub_contact_poll = None
        self._contact_poll_at = None
        _LOGGER.debug("Polling for the expected report")
        await self.async_request_refresh()

    def _live_tracked_dogs(self) -> set[str]:
        return {
            dog_id
            for dog_id, dog_data in (self.data or {}).items()
            if dog_data.live_tracking
        }

    @callback
    def _async_update_live_connection(self) -> None:
        """Stay connected to the live updates hub while any dog is tracked.

        Polling carries on as a fallback; the hub just delivers each report as
        soon as the monitor sends it.
        """
        dogs = self._live_tracked_dogs()
        if not dogs:
            if self._live_task is not None:
                _LOGGER.debug("Tracking ended, closing live updates")
                self._live_task.cancel()
                self._live_task = None
            return
        if self._live_task is None or self._live_task.done():
            self._live_task = self.config_entry.async_create_background_task(
                self.hass, self._async_run_live_connection(), "pitpat live updates"
            )
        elif self._live_connection is not None and self._live_connection.dogs != dogs:
            self.config_entry.async_create_background_task(
                self.hass,
                self._async_set_live_dogs(self._live_connection),
                "pitpat live updates subscriptions",
            )

    async def _async_run_live_connection(self) -> None:
        """Apply pushed updates until the connection drops.

        The next coordinator update reconnects if a dog is still tracked.
        Cancelled when tracking ends.
        """
        connection = PitPatLiveConnection(
            async_get_clientsession(self.hass),
            self.client,
            self.config_entry.data[CONF_USER_ID],
            self._async_live_monitor_changed,
        )
        connected = False
        try:
            await connection.async_connect()
            connected = True
            self._live_connection = connection
            self.update_interval = LIVE_CONNECTED_UPDATE_INTERVAL
            _LOGGER.debug("Connected to live updates")
            await connection.async_set_dogs(self._live_tracked_dogs())
            await connection.async_listen()
        except PitPatAuthError:
            self.config_entry.async_start_reauth(self.hass)
        except PitPatConnectionError as err:
            _LOGGER.debug("Live updates disconnected: %s", err)
        finally:
            self._live_connection = None
            await connection.async_close()
        if connected:
            self._async_live_connection_lost()

    @callback
    def _async_live_connection_lost(self) -> None:
        """Poll quickly again, and soon reconnect, if a dog is still tracked."""
        if not self._live_tracked_dogs():
            return
        self.update_interval = LIVE_UPDATE_INTERVAL
        if self._unsub_live_reconnect is None:
            self._unsub_live_reconnect = async_call_later(
                self.hass, LIVE_UPDATE_INTERVAL, self._async_live_reconnect
            )

    async def _async_live_reconnect(self, _now: datetime) -> None:
        self._unsub_live_reconnect = None
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        self._async_cancel_contact_poll()
        if self._unsub_live_reconnect:
            self._unsub_live_reconnect()
            self._unsub_live_reconnect = None
        if self._unsub_keep_alive:
            self._unsub_keep_alive()
            self._unsub_keep_alive = None
        await super().async_shutdown()

    async def _async_set_live_dogs(self, connection: PitPatLiveConnection) -> None:
        try:
            await connection.async_set_dogs(self._live_tracked_dogs())
        except PitPatConnectionError as err:
            # The listener notices the broken connection and gives up on it.
            _LOGGER.debug("Could not update live subscriptions: %s", err)

    @callback
    def _async_live_monitor_changed(self, dog_id: str, monitor: dict[str, Any]) -> None:
        if not self.data or dog_id not in self.data:
            return
        position = unwrap(monitor.get("LastKnownPosition")) or {}
        _LOGGER.debug(
            "Live update for %s, position from %s accurate to %s m, "
            "live tracking reason %s",
            self.data[dog_id].dog.get("Name"),
            position.get("DataTime"),
            (position.get("Accuracy") or {}).get("Metres"),
            monitor.get("LiveTrackingReason"),
        )
        # Unlike async_set_updated_data, this leaves the polling schedule alone,
        # so activity keeps refreshing during a walk.
        self.data = {**self.data, dog_id: replace(self.data[dog_id], monitor=monitor)}
        self.async_update_listeners()

    async def async_refresh_monitor(self, dog_id: str) -> None:
        """Fetch one dog's monitor now, e.g. after changing a setting.

        Errors are logged rather than raised: the change itself succeeded and
        the next scheduled update will catch up.
        """
        if not self.data or dog_id not in self.data:
            return
        try:
            monitor = await self.client.async_get_monitor(
                self.config_entry.data[CONF_USER_ID], dog_id
            )
        except PitPatAuthError:
            self.config_entry.async_start_reauth(self.hass)
            return
        except PitPatConnectionError as err:
            _LOGGER.debug("Could not refresh monitor for %s: %s", dog_id, err)
            return
        self.async_set_updated_data(
            {**self.data, dog_id: replace(self.data[dog_id], monitor=monitor)}
        )

    async def _async_update_data(self) -> dict[str, PitPatDogData]:
        user_id = self.config_entry.data[CONF_USER_ID]
        now = time.monotonic()
        live = any(dog_data.live_tracking for dog_data in (self.data or {}).values())
        full_interval = UPDATE_INTERVAL if live else FULL_UPDATE_INTERVAL
        full = (
            not self.data
            or now - self._last_full_update >= full_interval.total_seconds()
            or any(
                self._full_update_days.get(dog_id) != _today(dog_data.time_zone)
                for dog_id, dog_data in self.data.items()
            )
        )
        polled: dict[str, PitPatDogData] = {}
        try:
            if self.data:
                # Only the monitors change between reports.
                data = polled = await self._async_fetch_monitors(user_id)
                if reported := self._async_new_reports(data):
                    _LOGGER.debug(
                        "New report from %s, refreshing everything",
                        ", ".join(
                            data[dog_id].dog.get("Name", dog_id) for dog_id in reported
                        ),
                    )
                    for dog_id in reported:
                        self._detail_fetched_at.pop(dog_id, None)
                        self._walks_fetched_at.pop(dog_id, None)
                    full = True
            if full:
                dogs = await self.client.async_get_dogs(user_id)
                results = await asyncio.gather(
                    *(
                        self._async_fetch_dog(user_id, dog, polled.get(dog["Id"]))
                        for dog in dogs
                    )
                )
                data = {result.dog["Id"]: result for result in results}
                self._last_full_update = now
                self._full_update_reports = {
                    dog_id: dog_data.last_message_sent_at
                    for dog_id, dog_data in data.items()
                }
                self._full_update_days = {
                    dog_id: _today(dog_data.time_zone)
                    for dog_id, dog_data in data.items()
                }
        except PitPatAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PitPatConnectionError as err:
            raise UpdateFailed(str(err)) from err

        self._async_check_live_pushes(data)
        self._async_check_woken_up(data)
        fast = (
            now < self._fast_poll_until
            or bool(self._wake_up_reported_before)
            or any(dog_data.live_tracking for dog_data in data.values())
        )
        if not fast:
            self.update_interval = UPDATE_INTERVAL
        elif self._live_connection is not None:
            self.update_interval = LIVE_CONNECTED_UPDATE_INTERVAL
        else:
            self.update_interval = LIVE_UPDATE_INTERVAL
        return data

    @callback
    def _async_check_live_pushes(self, polled: dict[str, PitPatDogData]) -> None:
        """Reconnect if the hub has missed a report that polling found.

        As the app does: a subscription can stop delivering without the
        connection dropping.
        """
        connection = self._live_connection
        if connection is None or self._live_task is None or not self.data:
            return
        cutoff = dt_util.utcnow() - LIVE_PUSH_TIMEOUT
        for dog_id in connection.dogs:
            if dog_id not in polled or dog_id not in self.data:
                continue
            sent = polled[dog_id].last_message_sent_at
            known = self.data[dog_id].last_message_sent_at
            if sent and known and known < sent < cutoff:
                _LOGGER.debug(
                    "Live updates missed a report for %s, reconnecting",
                    self.data[dog_id].dog.get("Name"),
                )
                # The next listener update starts a new connection.
                self._live_task.cancel()
                self._live_task = None
                self._live_connection = None
                return

    async def _async_fetch_monitors(self, user_id: str) -> dict[str, PitPatDogData]:
        monitors = await asyncio.gather(
            *(self.client.async_get_monitor(user_id, dog_id) for dog_id in self.data)
        )
        return {
            dog_id: replace(dog_data, monitor=monitor)
            for (dog_id, dog_data), monitor in zip(
                self.data.items(), monitors, strict=True
            )
        }

    async def _async_fetch_dog(
        self, user_id: str, dog: dict[str, Any], polled: PitPatDogData | None
    ) -> PitPatDogData:
        """Fetch everything for a dog, reusing its monitor if just polled."""
        time_zone = await _async_time_zone(dog)
        today = _today(time_zone)
        yesterday = today - timedelta(days=1)

        monitor, months, detail, walks, needs_charging = await asyncio.gather(
            self._async_monitor(user_id, dog["Id"], polled),
            # Whole months come back, so this also covers yesterday on the 1st.
            self.client.async_get_activity(user_id, dog["Id"], yesterday, today),
            self._async_detailed_activity(user_id, dog["Id"], today),
            self._async_walks(user_id, dog["Id"], today),
            self._async_needs_charging(user_id, dog["Id"]),
        )

        days: dict[date, dict[str, Any]] = {}
        month = None
        for month_data in months or []:
            if month_data.get("Date", "")[:7] == today.isoformat()[:7]:
                month = month_data
            for day in month_data.get("ActivityDays") or []:
                try:
                    days[date.fromisoformat(day["Date"][:10])] = day
                except (KeyError, ValueError):
                    continue

        today_detail, total_steps, total_distance = detail
        return PitPatDogData(
            dog,
            monitor,
            time_zone=time_zone,
            today=days.get(today),
            yesterday=days.get(yesterday),
            month=month,
            today_detail=today_detail,
            today_detail_date=today if today_detail is not None else None,
            total_steps=total_steps,
            total_distance=total_distance,
            needs_charging=needs_charging,
            walks=walks,
            walks_date=today if walks is not None else None,
        )

    async def _async_monitor(
        self, user_id: str, dog_id: str, polled: PitPatDogData | None
    ) -> dict[str, Any] | None:
        if polled is not None:
            return polled.monitor
        return await self.client.async_get_monitor(user_id, dog_id)

    async def _async_detailed_activity(
        self, user_id: str, dog_id: str, today: date
    ) -> tuple[dict[str, Any] | None, int | None, float | None]:
        """Return (today's detailed totals, lifetime steps, lifetime distance).

        Reuses the previous result for up to DETAILED_ACTIVITY_INTERVAL, unless
        the day has changed. Failures only blank the sensors built on this
        call (e.g. just after midnight, before the day has any data) and are
        retried on the next full update.
        """
        previous = (self.data or {}).get(dog_id)
        fetched_at = self._detail_fetched_at.get(dog_id)
        if (
            previous is not None
            and previous.today_detail_date == today
            and fetched_at is not None
            and time.monotonic() - fetched_at
            < DETAILED_ACTIVITY_INTERVAL.total_seconds()
        ):
            return previous.today_detail, previous.total_steps, previous.total_distance

        try:
            combined, total_distance = await asyncio.gather(
                self.client.async_get_combined_activity(user_id, dog_id, today),
                self._async_total_distance(user_id, dog_id),
            )
        except PitPatConnectionError as err:
            _LOGGER.debug("Could not fetch detailed activity for %s: %s", dog_id, err)
            return None, None, None

        self._detail_fetched_at[dog_id] = time.monotonic()
        step_blocks = (combined or {}).get("StepBlocksForToday") or []
        total_steps = (
            step_blocks[-1].get("CumulativeStepCount") if step_blocks else None
        )
        return (combined or {}).get("ActivityDay"), total_steps, total_distance

    async def _async_total_distance(self, user_id: str, dog_id: str) -> float | None:
        try:
            result = await self.client.async_get_total_distance(user_id, dog_id)
        except PitPatConnectionError as err:
            _LOGGER.debug("Could not fetch total distance for %s: %s", dog_id, err)
            return None
        if not (result or {}).get("HasActivityData"):
            return None
        return result.get("DistanceInMeters")

    async def _async_walks(
        self, user_id: str, dog_id: str, today: date
    ) -> list[dict[str, Any]] | None:
        """Return today's walks, reusing the last result like detailed activity.

        Fetched on every full update while a walk is running, so it shows up
        soon after it ends.
        """
        previous = (self.data or {}).get(dog_id)
        fetched_at = self._walks_fetched_at.get(dog_id)
        if (
            previous is not None
            and previous.walks_date == today
            and fetched_at is not None
            and previous.monitor_value("LiveTrackingReason") != LIVE_TRACKING_WALK
            and time.monotonic() - fetched_at
            < DETAILED_ACTIVITY_INTERVAL.total_seconds()
        ):
            return previous.walks
        try:
            walks = await self.client.async_get_walks(user_id, dog_id, today)
        except PitPatConnectionError as err:
            _LOGGER.debug("Could not fetch walks for %s: %s", dog_id, err)
            return None
        self._walks_fetched_at[dog_id] = time.monotonic()
        return sorted(walks, key=lambda walk: walk.get("Start") or "")

    async def _async_needs_charging(self, user_id: str, dog_id: str) -> bool | None:
        try:
            return await self.client.async_needs_charging(user_id, dog_id)
        except PitPatConnectionError as err:
            _LOGGER.debug("Could not check charging for %s: %s", dog_id, err)
            return None


def _is_newer(sent: datetime | None, before: datetime | None) -> bool:
    """Whether a report was sent after the one before it (if any)."""
    return sent is not None and (before is None or sent > before)


async def _async_time_zone(dog: dict[str, Any]) -> tzinfo | None:
    """The dog's time zone, or None if it is missing or unknown."""
    name = dog.get("IanaTimeZoneName")
    if not name:
        return None
    try:
        return await dt_util.async_get_time_zone(name)
    except ValueError:
        # Not even a well-formed zone name.
        return None


def _today(time_zone: tzinfo | None) -> date:
    """The local date there; a dog's activity days are its calendar days."""
    return dt_util.now(time_zone).date()
