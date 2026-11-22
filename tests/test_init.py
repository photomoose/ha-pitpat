"""Tests for PitPat setup and sensors."""

import copy
import re

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import CONF_EMAIL, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_pitpat.const import (
    API_BASE_URL,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
    TOKEN_URL,
)

from freezegun.api import FrozenDateTimeFactory

from custom_components.ha_pitpat.const import ACTIVITY_BASE_URL, LOCATION_BASE_URL
from custom_components.ha_pitpat.binary_sensor import BINARY_SENSORS
from custom_components.ha_pitpat.coordinator import (
    KEEP_ALIVE_LIMIT,
    LIVE_START_GRACE,
    WAKE_UP_POLL,
)
from custom_components.ha_pitpat.geocode import NOMINATIM_URL
from custom_components.ha_pitpat.sensor import SENSORS

from .conftest import (
    ACTIVITY_RESPONSE,
    COMBINED_RESPONSE,
    DOG,
    DOG_ID,
    MONITOR_RESPONSE,
    USER_ID,
    FakeLive,
    activity_day,
)

TOKENS = {
    "access_token": "access",
    "refresh_token": "refresh-2",
    "expires_in": 3600,
    "token_type": "Bearer",
}


ACTIVITY_URL = (
    f"{ACTIVITY_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/ActivityMonthAndDaysByMonth"
)


TOTAL_DISTANCE_URL = (
    f"{ACTIVITY_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/GetTotalDistance"
)
TOTAL_DISTANCE_RESPONSE = {"DistanceInMeters": 1234567.0, "HasActivityData": True}

NOMINATIM_RESPONSE = {
    "display_name": "10, Test Road, Testville, Testshire, England, AB1 2CD, United Kingdom",
    "address": {
        "house_number": "10",
        "road": "Test Road",
        "suburb": "Testville",
        "city": "Testchester",
        "postcode": "AB1 2CD",
        "country": "United Kingdom",
    },
}

COMBINED_URL = re.compile(
    rf"^{re.escape(ACTIVITY_BASE_URL)}/api/Users/{USER_ID}/Dogs/{DOG_ID}"
    r"/CombinedActivity/"
)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        data={
            CONF_EMAIL: "me@example.com",
            CONF_USER_ID: USER_ID,
            CONF_REFRESH_TOKEN: "refresh-1",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_creates_dog_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(ACTIVITY_URL, json=ACTIVITY_RESPONSE)
    aioclient_mock.get(COMBINED_URL, json=COMBINED_RESPONSE)
    aioclient_mock.get(TOTAL_DISTANCE_URL, json=TOTAL_DISTANCE_RESPONSE)
    _mock_location_extras(aioclient_mock)
    aioclient_mock.get(NOMINATIM_URL, json=NOMINATIM_RESPONSE)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json=MONITOR_RESPONSE,
    )
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # Cold start refreshes the access token and persists the rotated refresh token.
    token_call, dogs_call, *dog_calls = aioclient_mock.mock_calls
    activity_call = next(c for c in dog_calls if c[1].path.endswith("ByMonth"))
    combined_call = next(c for c in dog_calls if "/CombinedActivity/" in c[1].path)
    assert combined_call[1].path.endswith(
        "/CombinedActivity/2026-11-16T00:00:00.0000000Z"
    )
    assert activity_call[1].query == {
        "startDate": "2026-11-15T00:00:00.0000000Z",
        "endDate": "2026-11-16T00:00:00.0000000Z",
        "locale": "en-GB",
    }
    assert activity_call[3]["Authorization"] == "Bearer access"
    assert token_call[2] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": "PitPatAppSso",
    }
    assert dogs_call[3]["Authorization"] == "Bearer access"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-2"

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DOG_ID), entry.entry_id
    )
    assert device.name == "Rex"
    assert device.sw_version == "1.4.4"
    assert device.hw_version == "6.3.5"
    assert device.serial_number == "TESTSERIAL"

    def state(entity_id: str) -> str:
        return hass.states.get(entity_id).state

    assert state("sensor.rex_weight") == "12.5"
    assert state("sensor.rex_daily_exercise_goal") == "60"
    assert state("sensor.rex_exercise_today") == "43"
    assert state("sensor.rex_pottering_today") == "1"
    assert state("sensor.rex_resting_today") == "1051"
    assert state("sensor.rex_exercise_goal_progress") == "72"
    assert state("sensor.rex_walking_today") == "21"
    assert state("sensor.rex_running_today") == "46"
    assert state("sensor.rex_playing_today") == "7"
    assert state("sensor.rex_steps_today") == "12000"
    assert state("sensor.rex_total_steps") == "1012000"
    total_distance = hass.states.get("sensor.rex_total_distance")
    assert float(total_distance.state) == pytest.approx(1234.567)
    assert total_distance.attributes["unit_of_measurement"] == "km"
    assert state("sensor.rex_calories_today") == "340"
    distance = hass.states.get("sensor.rex_distance_today")
    assert float(distance.state) == 5.0
    assert distance.attributes["unit_of_measurement"] == "km"
    assert state("sensor.rex_activity_data_up_to") == "2026-11-16T17:45:00+00:00"
    assert state("sensor.rex_exercise_yesterday") == "62"
    assert state("sensor.rex_exercise_goals_met_this_month") == "11"
    assert state("binary_sensor.rex_exercise_goal_met_today") == "off"
    assert state("sensor.rex_battery") == "93"
    assert state("sensor.rex_last_activity") == "2026-11-16T18:10:00+00:00"
    assert state("sensor.rex_last_sync") == "2026-11-16T18:13:38+00:00"
    assert state("sensor.rex_next_expected_contact") == "2026-11-16T18:33:38+00:00"
    assert state("sensor.rex_location_accuracy") == "14.0"
    assert state("sensor.rex_last_location_fix") == "2026-11-16T13:09:35+00:00"
    assert hass.states.get("sensor.rex_signal_strength") is None  # disabled
    assert state("sensor.rex_network_quality") == "fair"
    assert hass.states.get("sensor.rex_network_operator") is None  # disabled
    assert state("binary_sensor.rex_charging") == "off"
    assert state("binary_sensor.rex_lost_contact") == "off"

    tracker = hass.states.get("device_tracker.rex")
    assert tracker.state == "not_home"
    assert tracker.attributes["latitude"] == 51.5
    assert tracker.attributes["longitude"] == -0.1
    assert tracker.attributes["gps_accuracy"] == 14.0
    assert tracker.attributes["position_time"] == "2026-11-16T13:09:35Z"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_dog_without_monitor(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(ACTIVITY_URL, json=[])
    aioclient_mock.get(COMBINED_URL, status=404)
    aioclient_mock.get(TOTAL_DISTANCE_URL, json=TOTAL_DISTANCE_RESPONSE)
    _mock_location_extras(aioclient_mock)
    aioclient_mock.get(NOMINATIM_URL, json=NOMINATIM_RESPONSE)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json={"HasValue": False},
    )
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.rex_weight").state == "12.5"
    assert hass.states.get("sensor.rex_battery").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.rex_lost_contact").state == STATE_UNKNOWN
    assert hass.states.get("device_tracker.rex").state == STATE_UNKNOWN
    assert hass.states.get("sensor.rex_exercise_today").state == STATE_UNKNOWN
    # A failing detailed activity call only blanks its own sensors.
    assert hass.states.get("sensor.rex_steps_today").state == STATE_UNKNOWN
    assert hass.states.get("sensor.rex_total_steps").state == STATE_UNKNOWN


async def test_activity_uses_dog_local_day_across_month_boundary(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    # 23:30 UTC on 30 Nov is already 1 Dec in Sydney.
    freezer.move_to("2026-11-30T23:30:00+00:00")
    dog = {**DOG, "IanaTimeZoneName": "Australia/Sydney"}
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[dog])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json={"HasValue": False},
    )
    aioclient_mock.get(
        ACTIVITY_URL,
        json=[
            {
                "ExerciseGoalsMet": 0,
                "Date": "2026-12-01T00:00:00Z",
                "ActivityDays": [activity_day("2026-12-01", 5, False)],
            },
            {
                "ExerciseGoalsMet": 20,
                "Date": "2026-11-01T00:00:00Z",
                "ActivityDays": [activity_day("2026-11-30", 75, True)],
            },
        ],
    )
    aioclient_mock.get(COMBINED_URL, json=COMBINED_RESPONSE)
    aioclient_mock.get(TOTAL_DISTANCE_URL, json=TOTAL_DISTANCE_RESPONSE)
    _mock_location_extras(aioclient_mock)
    aioclient_mock.get(NOMINATIM_URL, json=NOMINATIM_RESPONSE)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    combined_call = next(
        c for c in aioclient_mock.mock_calls if "/CombinedActivity/" in c[1].path
    )
    assert combined_call[1].path.endswith(
        "/CombinedActivity/2026-12-01T00:00:00.0000000Z"
    )
    activity_call = next(
        c for c in aioclient_mock.mock_calls if c[1].path.endswith("ByMonth")
    )
    assert activity_call[1].query["startDate"] == "2026-11-30T00:00:00.0000000Z"
    assert activity_call[1].query["endDate"] == "2026-12-01T00:00:00.0000000Z"
    assert hass.states.get("sensor.rex_exercise_today").state == "5"
    assert hass.states.get("sensor.rex_exercise_yesterday").state == "75"
    assert hass.states.get("sensor.rex_exercise_goals_met_this_month").state == "0"


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


START_WALK_URL = (
    f"{LOCATION_BASE_URL}/api/2/user/{USER_ID}/dog/{DOG_ID}/livetracking/start/walk"
)
START_FIND_URL = (
    f"{LOCATION_BASE_URL}/api/user/{USER_ID}/dog/{DOG_ID}/livetracking/start/find"
)
STOP_URL = (
    f"{LOCATION_BASE_URL}/api/user/{USER_ID}/dog/{DOG_ID}/livetracking/stop"
)
KEEP_ALIVE_URL = (
    f"{LOCATION_BASE_URL}/api/2/user/{USER_ID}/dog/{DOG_ID}/monitor/keepAlive"
)
WAKE_UP_URL = f"{LOCATION_BASE_URL}/api/user/{USER_ID}/monitors/wakeup?source=Other"
WALKS_URL = (
    f"{LOCATION_BASE_URL}/api/user/{USER_ID}/dog/{DOG_ID}/tracking/day/recorded"
)
BATTERY_CHECK_URL = (
    f"{LOCATION_BASE_URL}/api/user/{USER_ID}/dog/{DOG_ID}/monitor/batterycheck"
)


def _walk(start: str, end: str, steps: int) -> dict:
    return {
        "Type": 0,
        "Start": f"2026-11-16T{start}:00Z",
        "End": f"2026-11-16T{end}:00Z",
        "TimeZoneName": "Europe/London",
        "Summary": {
            "HasValue": True,
            "Value": {
                "Vicinity": "Testville",
                "Activity": {
                    "HasValue": True,
                    "Value": {
                        "TotalKCalBurned": 120,
                        "TotalSteps": steps,
                        "TotalDistance": {"Metres": 3200.0},
                    },
                },
            },
        },
        "LocationFixes": [],
    }


# Out of order, to check the latest is picked.
WALKS_RESPONSE = {
    "Date": "2026-11-16",
    "Sessions": [_walk("17:00", "17:40", 5000), _walk("08:00", "08:30", 3000)],
    "Background": [],
    "DaysSinceToday": 0,
}


def _monitor(reason: int, latitude: float = 51.5) -> dict:
    monitor = MONITOR_RESPONSE["Value"]["Monitor"]
    position = monitor["LastKnownPosition"]["Value"]
    return {
        "HasValue": True,
        "Value": {
            "DogId": DOG_ID,
            "Monitor": {
                **monitor,
                "LiveTrackingReason": reason,
                "LastKnownPosition": {
                    "HasValue": True,
                    "Value": {**position, "Latitude": latitude},
                },
            },
        },
    }


def _mock_endpoints(
    aioclient_mock: AiohttpClientMocker,
    monitor: dict,
    combined: dict = COMBINED_RESPONSE,
    geocode: dict = NOMINATIM_RESPONSE,
    walks: dict | None = WALKS_RESPONSE,
    needs_charging: bool | None = False,
    wake_up: bool = True,
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors", json=monitor
    )
    aioclient_mock.get(ACTIVITY_URL, json=ACTIVITY_RESPONSE)
    aioclient_mock.get(COMBINED_URL, json=combined)
    aioclient_mock.get(TOTAL_DISTANCE_URL, json=TOTAL_DISTANCE_RESPONSE)
    aioclient_mock.get(NOMINATIM_URL, json=geocode)
    aioclient_mock.put(START_WALK_URL, text="")
    aioclient_mock.put(START_FIND_URL, text="")
    aioclient_mock.put(STOP_URL, text="")
    aioclient_mock.put(KEEP_ALIVE_URL, text="")
    aioclient_mock.put(WAKE_UP_URL, json=wake_up)
    if walks is None:
        aioclient_mock.get(WALKS_URL, status=500)
    else:
        aioclient_mock.get(WALKS_URL, json=walks)
    if needs_charging is None:
        aioclient_mock.get(BATTERY_CHECK_URL, status=500)
    else:
        aioclient_mock.get(BATTERY_CHECK_URL, json=needs_charging)


def _mock_location_extras(aioclient_mock: AiohttpClientMocker) -> None:
    """Walks and charging, for tests that mock the other endpoints by hand."""
    aioclient_mock.get(WALKS_URL, json=WALKS_RESPONSE)
    aioclient_mock.get(BATTERY_CHECK_URL, json=False)


def _hosts(aioclient_mock: AiohttpClientMocker) -> list[str]:
    """Endpoints called on PitPat (address lookups are left out)."""
    return [
        c[1].path.rsplit("/", 1)[-1]
        for c in aioclient_mock.mock_calls
        if not str(c[1]).startswith(NOMINATIM_URL)
    ]


async def test_fast_polling_during_walk(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(2))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert hass.states.get("sensor.rex_tracking_mode").state == "walk"
    assert coordinator.update_interval.total_seconds() == 30

    # A fast tick refreshes only the monitor, picking up the new position.
    _mock_endpoints(aioclient_mock, _monitor(2, latitude=51.6))
    freezer.tick(30)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _hosts(aioclient_mock) == ["Monitors"]
    assert hass.states.get("device_tracker.rex").attributes["latitude"] == 51.6

    # Once the walk ends, polling drops back to the normal interval.
    _mock_endpoints(aioclient_mock, _monitor(0))
    freezer.tick(30)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.rex_tracking_mode").state == "off"
    assert coordinator.update_interval.total_seconds() == 300

    # Without a new report, only the monitor is polled.
    _mock_endpoints(aioclient_mock, _monitor(0))
    await _tick(hass, freezer, 300)
    assert _hosts(aioclient_mock) == ["Monitors"]


async def test_full_update_after_each_report(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Between reports only the monitor is polled.
    for _ in range(3):
        _mock_endpoints(aioclient_mock, _monitor(0))
        await _tick(hass, freezer, 300)
        assert _hosts(aioclient_mock) == ["Monitors"]

    # A report brings new activity, so everything is fetched, detailed
    # activity and walks included.
    _mock_endpoints(aioclient_mock, _reported_at("2026-11-16T18:33:38Z"))
    await _tick(hass, freezer, 300)
    assert sorted(_hosts(aioclient_mock)) == [
        "2026-11-16T00:00:00.0000000Z",  # detailed activity
        "ActivityMonthAndDaysByMonth",
        "Dogs",
        "GetTotalDistance",
        "Monitors",
        "batterycheck",
        "recorded",
    ]

    # Without reports, everything is still fetched once an hour.
    _mock_endpoints(aioclient_mock, _reported_at("2026-11-16T18:33:38Z"))
    for _ in range(11):
        await _tick(hass, freezer, 300)
    assert "Dogs" not in _hosts(aioclient_mock)
    await _tick(hass, freezer, 300)
    assert "Dogs" in _hosts(aioclient_mock)


CADENCE_URL = (
    f"{LOCATION_BASE_URL}/api/user/{USER_ID}/dog/{DOG_ID}"
    "/monitor/updatePermanentCadence"
)


def _monitor_with_cadence(cadence: int) -> dict:
    monitor = _monitor(0)
    monitor["Value"]["Monitor"]["PhoneHomeCadence"] = cadence
    return monitor


async def _select_gps_mode(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.rex_gps_mode", "option": option},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_gps_mode_select(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-17T19:30:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(0))
    aioclient_mock.put(CADENCE_URL, text="")
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("select.rex_gps_mode")
    assert state.state == "standard"
    assert state.attributes["options"] == ["economy", "standard", "urgent"]

    aioclient_mock.mock_calls.clear()
    await _select_gps_mode(hass, "economy")
    # The change is followed straight away by a read of that dog's monitor only.
    (put_call, monitor_call) = aioclient_mock.mock_calls
    assert put_call[0] == "PUT"
    assert put_call[1].path.endswith("/monitor/updatePermanentCadence")
    assert put_call[1].query == {"cadence": "Economy"}
    assert monitor_call[0] == "GET"
    assert monitor_call[1].path.endswith(f"/Dogs/{DOG_ID}/Monitors")

    # The monitor still reports Standard, but the requested mode is shown.
    assert hass.states.get("select.rex_gps_mode").state == "economy"

    # Once the monitor reports the new cadence, that is what is shown.
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(1))
    aioclient_mock.put(CADENCE_URL, text="")
    freezer.tick(300)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("select.rex_gps_mode").state == "economy"

    # A later change made elsewhere (e.g. in the app) is picked up.
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(2))
    freezer.tick(300)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("select.rex_gps_mode").state == "urgent"


async def test_gps_mode_pending_expires(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-17T19:30:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(0))
    aioclient_mock.put(CADENCE_URL, text="")
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await _select_gps_mode(hass, "urgent")
    assert hass.states.get("select.rex_gps_mode").state == "urgent"

    # The monitor never reports the change: fall back after an hour.
    freezer.tick(3600)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("select.rex_gps_mode").state == "standard"


async def test_gps_mode_select_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(0))
    aioclient_mock.put(CADENCE_URL, status=500)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError, match="Could not set GPS mode"):
        await _select_gps_mode(hass, "urgent")
    assert hass.states.get("select.rex_gps_mode").state == "standard"


async def test_gps_mode_unknown_cadence(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(7))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("select.rex_gps_mode").state == STATE_UNKNOWN


async def test_gps_mode_reported_immediately(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """If the server applies the cadence at once, the read-back shows it."""
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    _mock_endpoints(aioclient_mock, _monitor_with_cadence(2))
    aioclient_mock.put(CADENCE_URL, text="")
    await _select_gps_mode(hass, "urgent")

    assert hass.states.get("select.rex_gps_mode").state == "urgent"
    assert entry.runtime_data.data[DOG_ID].monitor["PhoneHomeCadence"] == 2


async def test_gps_mode_read_back_failure_is_not_an_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor_with_cadence(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.put(CADENCE_URL, text="")
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors", status=500
    )
    await _select_gps_mode(hass, "economy")

    # The change went through, so the requested mode is still shown.
    assert hass.states.get("select.rex_gps_mode").state == "economy"


BACKGROUND_TRACKING_URL = (
    f"{LOCATION_BASE_URL}/api/user/{USER_ID}/dog/{DOG_ID}"
    "/monitor/updateBackgroundTracking"
)


def _monitor_with_background_tracking(enabled: bool) -> dict:
    monitor = _monitor(0)
    monitor["Value"]["Monitor"]["BackgroundTrackingEnabled"] = enabled
    return monitor


async def _switch_background_tracking(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        "switch",
        service,
        {"entity_id": "switch.rex_background_tracking"},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_background_tracking_switch(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-17T19:45:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor_with_background_tracking(False))
    aioclient_mock.put(BACKGROUND_TRACKING_URL, text="")
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.rex_background_tracking").state == "off"

    aioclient_mock.mock_calls.clear()
    await _switch_background_tracking(hass, "turn_on")
    (put_call, monitor_call) = aioclient_mock.mock_calls
    assert put_call[0] == "PUT"
    assert put_call[1].path.endswith("/monitor/updateBackgroundTracking")
    assert put_call[1].query == {"isEnabled": "True"}
    assert monitor_call[1].path.endswith(f"/Dogs/{DOG_ID}/Monitors")
    # The monitor still reports off; the requested state is shown meanwhile.
    assert hass.states.get("switch.rex_background_tracking").state == "on"

    # The monitor catches up on a later update.
    _mock_endpoints(aioclient_mock, _monitor_with_background_tracking(True))
    aioclient_mock.put(BACKGROUND_TRACKING_URL, text="")
    freezer.tick(300)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("switch.rex_background_tracking").state == "on"

    # Turning off: the read-back still reports on, so the requested off shows.
    aioclient_mock.mock_calls.clear()
    await _switch_background_tracking(hass, "turn_off")
    put_call = next(c for c in aioclient_mock.mock_calls if c[0] == "PUT")
    assert put_call[1].query == {"isEnabled": "False"}
    assert hass.states.get("switch.rex_background_tracking").state == "off"

    # When the monitor reports off, the switch stays off.
    _mock_endpoints(aioclient_mock, _monitor_with_background_tracking(False))
    freezer.tick(300)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("switch.rex_background_tracking").state == "off"


async def test_background_tracking_switch_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor_with_background_tracking(False))
    aioclient_mock.put(BACKGROUND_TRACKING_URL, status=500)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(
        HomeAssistantError, match="Could not change background tracking"
    ):
        await _switch_background_tracking(hass, "turn_on")
    assert hass.states.get("switch.rex_background_tracking").state == "off"


def _combined_calls(aioclient_mock: AiohttpClientMocker) -> list[str]:
    return [
        c[1].path.rsplit("/", 1)[-1]
        for c in aioclient_mock.mock_calls
        if "/CombinedActivity/" in c[1].path
    ]


async def _tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int):
    freezer.tick(seconds)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_detailed_activity_every_15_minutes_during_walk(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    # 23:40 in London, so the day changes during the test.
    freezer.move_to("2026-11-16T23:40:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(2))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _combined_calls(aioclient_mock) == ["2026-11-16T00:00:00.0000000Z"]
    assert hass.states.get("sensor.rex_steps_today").state == "12000"

    # 23:45 and 23:50: during a walk everything is fetched every five
    # minutes, but the detailed activity is reused.
    for _ in range(2):
        aioclient_mock.mock_calls.clear()
        await _tick(hass, freezer, 300)
        assert "Dogs" in _hosts(aioclient_mock)
        assert _combined_calls(aioclient_mock) == []
        assert hass.states.get("sensor.rex_steps_today").state == "12000"

    # 23:55: fifteen minutes since the last fetch.
    aioclient_mock.mock_calls.clear()
    await _tick(hass, freezer, 300)
    assert _combined_calls(aioclient_mock) == ["2026-11-16T00:00:00.0000000Z"]


async def test_full_update_on_new_day(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    # 23:55 in London.
    freezer.move_to("2026-11-16T23:55:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # 00:00: no new report, but a new day, so today's sensors start afresh.
    new_day = {
        **COMBINED_RESPONSE,
        "ActivityDay": {**COMBINED_RESPONSE["ActivityDay"], "TotalSteps": 12},
    }
    _mock_endpoints(aioclient_mock, _monitor(0), combined=new_day)
    await _tick(hass, freezer, 300)
    assert _combined_calls(aioclient_mock) == ["2026-11-17T00:00:00.0000000Z"]
    assert hass.states.get("sensor.rex_steps_today").state == "12"


async def test_detailed_activity_retried_after_failure(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:00:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json=_monitor(0),
    )
    aioclient_mock.get(ACTIVITY_URL, json=ACTIVITY_RESPONSE)
    aioclient_mock.get(COMBINED_URL, status=500)
    aioclient_mock.get(TOTAL_DISTANCE_URL, json=TOTAL_DISTANCE_RESPONSE)
    _mock_location_extras(aioclient_mock)
    aioclient_mock.get(NOMINATIM_URL, json=NOMINATIM_RESPONSE)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.rex_steps_today").state == STATE_UNKNOWN

    # The next report tries again.
    _mock_endpoints(aioclient_mock, _reported_at("2026-11-16T18:20:00Z"))
    await _tick(hass, freezer, 300)
    assert len(_combined_calls(aioclient_mock)) == 1
    assert hass.states.get("sensor.rex_steps_today").state == "12000"


async def _switch(hass: HomeAssistant, entity_id: str, service: str) -> None:
    await hass.services.async_call(
        "switch", service, {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)


def _puts(aioclient_mock: AiohttpClientMocker) -> list[str]:
    return [str(c[1]) for c in aioclient_mock.mock_calls if c[0] == "PUT"]


async def test_start_walk_button(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.update_interval.total_seconds() == 300

    # Starting a walk reads the monitor back and polls quickly while it wakes.
    aioclient_mock.mock_calls.clear()
    await _press(hass, "button.rex_start_a_walk")
    assert _puts(aioclient_mock) == [START_WALK_URL]
    assert _hosts(aioclient_mock) == ["walk", "Monitors"]
    assert coordinator.update_interval.total_seconds() == 30

    _mock_endpoints(aioclient_mock, _monitor(2))
    await _tick(hass, freezer, 30)
    assert hass.states.get("sensor.rex_tracking_mode").state == "walk"

    # Stopping ends the walk and stops the start-up fast polling.
    aioclient_mock.mock_calls.clear()
    _mock_endpoints(aioclient_mock, _monitor(0))
    await _press(hass, "button.rex_stop_live_tracking")
    assert _puts(aioclient_mock) == [STOP_URL]
    assert hass.states.get("sensor.rex_tracking_mode").state == "off"
    await _tick(hass, freezer, 30)
    assert coordinator.update_interval.total_seconds() == 300


async def test_start_live_tracking_button(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    aioclient_mock.mock_calls.clear()
    await _press(hass, "button.rex_start_live_tracking")
    assert _puts(aioclient_mock) == [START_FIND_URL]

    _mock_endpoints(aioclient_mock, _monitor(1))
    await _tick(hass, freezer, 30)
    assert hass.states.get("sensor.rex_tracking_mode").state == "live_tracking"

    # It stays fast beyond the start-up grace period while tracking continues.
    for _ in range(int(LIVE_START_GRACE.total_seconds() // 30) + 1):
        await _tick(hass, freezer, 30)
    assert entry.runtime_data.update_interval.total_seconds() == 30


async def test_fast_polling_ends_if_monitor_never_starts(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    await _press(hass, "button.rex_start_a_walk")
    ticks = int(LIVE_START_GRACE.total_seconds() // 30)

    # Still waiting for the monitor just before the grace period ends...
    for _ in range(ticks - 1):
        await _tick(hass, freezer, 30)
    assert coordinator.update_interval.total_seconds() == 30

    # ...then polling slows down.
    await _tick(hass, freezer, 30)
    assert coordinator.update_interval.total_seconds() == 300


async def test_tracking_button_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor(0))
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json=_monitor(0),
    )
    aioclient_mock.get(ACTIVITY_URL, json=ACTIVITY_RESPONSE)
    aioclient_mock.get(COMBINED_URL, json=COMBINED_RESPONSE)
    aioclient_mock.get(TOTAL_DISTANCE_URL, json=TOTAL_DISTANCE_RESPONSE)
    _mock_location_extras(aioclient_mock)
    aioclient_mock.get(NOMINATIM_URL, json=NOMINATIM_RESPONSE)
    aioclient_mock.put(START_WALK_URL, status=500)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError, match="Could not start a walk"):
        await _press(hass, "button.rex_start_a_walk")
    assert entry.runtime_data.update_interval.total_seconds() == 300


async def test_entity_categories(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Settings appear under Configuration; tracking buttons under Controls."""
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for entity_id in ("select.rex_gps_mode", "switch.rex_background_tracking"):
        assert registry.async_get(entity_id).entity_category is EntityCategory.CONFIG
    for entity_id in (
        "button.rex_start_live_tracking",
        "button.rex_start_a_walk",
        "button.rex_stop_live_tracking",
        "sensor.rex_location_accuracy",
        "sensor.rex_tracking_status",
        "sensor.rex_tracking_mode",
    ):
        assert registry.async_get(entity_id).entity_category is None


async def test_no_duplicate_tracking_binary_sensors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The tracking buttons and sensors replace the old binary sensors."""
    _mock_endpoints(aioclient_mock, _monitor(2))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.rex_walk_in_progress") is None
    assert hass.states.get("binary_sensor.rex_live_tracking") is None


async def test_live_updates_while_tracking(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    mock_live: FakeLive,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert mock_live.connections == []

    # Once the monitor reports tracking, the hub is subscribed to the dog.
    _mock_endpoints(aioclient_mock, _monitor(2))
    await _tick(hass, freezer, 300)
    connection = mock_live.connections[-1]
    assert connection.dogs == {DOG_ID}

    # Pushed reports update the tracker straight away; unknown dogs are ignored.
    connection.on_monitor(DOG_ID, _monitor(2, latitude=51.7)["Value"]["Monitor"])
    connection.on_monitor("another-dog", _monitor(2)["Value"]["Monitor"])
    await hass.async_block_till_done()
    assert hass.states.get("device_tracker.rex").attributes["latitude"] == 51.7
    assert set(entry.runtime_data.data) == {DOG_ID}

    # A dropped connection means polling quickly again, and reconnecting.
    assert entry.runtime_data.update_interval.total_seconds() == 30
    count = len(mock_live.connections)
    await _tick(hass, freezer, 30)
    assert len(mock_live.connections) == count + 1


async def test_live_updates_stop_with_tracking(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    mock_live: FakeLive,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    mock_live.stay_connected = True
    _mock_endpoints(aioclient_mock, _monitor(2))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [connection] = mock_live.connections
    assert connection.dogs == {DOG_ID}
    assert not connection.closed

    # The hub reports that tracking has ended, so the connection is closed.
    connection.on_monitor(DOG_ID, _monitor(0)["Value"]["Monitor"])
    await hass.async_block_till_done()
    assert connection.closed
    assert hass.states.get("sensor.rex_tracking_mode").state == "off"
    assert len(mock_live.connections) == 1


async def test_tracking_status(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    mock_live: FakeLive,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    mock_live.stay_connected = True
    _mock_endpoints(aioclient_mock, _monitor(1))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.rex_tracking_status").state == "idle"

    # Pushed as the collar wakes up and gets a fix, as in the app.
    [connection] = mock_live.connections
    for state, expected in (
        (1, "waiting_for_contact"),
        (2, "waiting_for_satellites"),
        (3, "tracking"),
        (9, STATE_UNKNOWN),
    ):
        monitor = _monitor(1)["Value"]["Monitor"]
        monitor["GpsSynchronisationState"] = state
        connection.on_monitor(DOG_ID, monitor)
        await hass.async_block_till_done()
        assert hass.states.get("sensor.rex_tracking_status").state == expected


async def test_slow_polling_while_live_updates_connected(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    mock_live: FakeLive,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    mock_live.stay_connected = True
    _mock_endpoints(aioclient_mock, _monitor(2))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [connection] = mock_live.connections

    # While connected, polling is only a fallback. (The live connection
    # never finishes here, so don't wait for background tasks.)
    coordinator = entry.runtime_data
    assert coordinator.update_interval.total_seconds() == 120
    aioclient_mock.mock_calls.clear()
    freezer.tick(120)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _hosts(aioclient_mock) == ["Monitors"]
    assert coordinator.update_interval.total_seconds() == 120
    aioclient_mock.mock_calls.clear()
    freezer.tick(60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _hosts(aioclient_mock) == []
    assert mock_live.connections == [connection]


async def test_keep_alive_after_starting_live_tracking(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Walks run without keepAlives.
    _mock_endpoints(aioclient_mock, _monitor(2))
    await _press(hass, "button.rex_start_a_walk")
    await _tick(hass, freezer, 30)
    assert KEEP_ALIVE_URL not in _puts(aioclient_mock)

    # Live tracking to find the dog is kept going every 30 seconds...
    _mock_endpoints(aioclient_mock, _monitor(1))
    await _press(hass, "button.rex_start_live_tracking")
    for _ in range(3):
        await _tick(hass, freezer, 30)
    assert _puts(aioclient_mock).count(KEEP_ALIVE_URL) == 3

    # ...until it ends, here from the app.
    _mock_endpoints(aioclient_mock, _monitor(0))
    for _ in range(3):
        await _tick(hass, freezer, 30)
    assert _puts(aioclient_mock).count(KEEP_ALIVE_URL) <= 1


async def test_keep_alive_stops_with_stop_button_and_limit(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(1))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Stopping from Home Assistant ends the keepAlives straight away.
    await _press(hass, "button.rex_start_live_tracking")
    await _press(hass, "button.rex_stop_live_tracking")
    aioclient_mock.mock_calls.clear()
    await _tick(hass, freezer, 30)
    assert KEEP_ALIVE_URL not in _puts(aioclient_mock)

    # Otherwise they end at the limit, even if tracking carries on.
    await _press(hass, "button.rex_start_live_tracking")
    freezer.tick(KEEP_ALIVE_LIMIT.total_seconds())
    await _tick(hass, freezer, 30)
    aioclient_mock.mock_calls.clear()
    await _tick(hass, freezer, 30)
    assert KEEP_ALIVE_URL not in _puts(aioclient_mock)


async def test_reconnects_when_live_updates_miss_a_report(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    mock_live: FakeLive,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    mock_live.stay_connected = True
    _mock_endpoints(aioclient_mock, _monitor(2))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [connection] = mock_live.connections

    coordinator = entry.runtime_data

    # A poll finding the report pushed is fine. (The connection never
    # finishes here, so polls are run directly rather than waited for.)
    freezer.tick(60)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert mock_live.connections == [connection]

    # A poll finding a report sent a minute ago that never arrived reconnects.
    missed = copy.deepcopy(_monitor(2))
    missed["Value"]["Monitor"]["ContactTimings"]["Value"][
        "LastMessageSentAt"
    ] = "2026-11-16T18:15:00Z"
    _mock_endpoints(aioclient_mock, missed)
    freezer.tick(60)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert connection.closed
    assert len(mock_live.connections) == 2


def _reported_at(sent: str) -> dict:
    monitor = copy.deepcopy(_monitor(0))
    timings = monitor["Value"]["Monitor"]["ContactTimings"]["Value"]
    timings["LastMessageSentAt"] = sent
    timings["LastMessageReceivedAt"] = sent
    return monitor


async def test_request_update_button(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    aioclient_mock.mock_calls.clear()
    await _press(hass, "button.rex_request_update")
    assert _puts(aioclient_mock) == [WAKE_UP_URL]
    assert _hosts(aioclient_mock) == ["wakeup", "Monitors"]
    put = next(c for c in aioclient_mock.mock_calls if c[0] == "PUT")
    assert put[1].query["source"] == "Other"
    # Polls quickly while waiting for the monitor to report in.
    assert coordinator.update_interval.total_seconds() == 30
    aioclient_mock.mock_calls.clear()
    await _tick(hass, freezer, 30)
    assert _hosts(aioclient_mock) == ["Monitors"]

    # The report brings new activity, so everything is refreshed at once,
    # detailed activity included, and polling slows down again.
    _mock_endpoints(aioclient_mock, _reported_at("2026-11-16T18:15:40Z"))
    await _tick(hass, freezer, 30)
    assert _hosts(aioclient_mock)[0] == "Monitors"
    urls = [str(c[1]) for c in aioclient_mock.mock_calls]
    for endpoint in ("/Dogs", "CombinedActivity", "tracking/day", "batterycheck"):
        assert any(endpoint in url for url in urls), endpoint
    assert coordinator.update_interval.total_seconds() == 300

    # Without a report, quick polling stops after a few minutes.
    await _press(hass, "button.rex_request_update")
    assert coordinator.update_interval.total_seconds() == 30
    for _ in range(int(WAKE_UP_POLL.total_seconds() // 30) + 1):
        await _tick(hass, freezer, 30)
    assert coordinator.update_interval.total_seconds() == 300

    _mock_endpoints(aioclient_mock, _monitor(0), wake_up=False)
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await _press(hass, "button.rex_request_update")


async def test_walks_and_charging(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(0), walks=WALKS_RESPONSE, needs_charging=True)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.rex_needs_charging").state == "on"
    assert hass.states.get("sensor.rex_walks_today").state == "2"
    last_walk = hass.states.get("sensor.rex_last_walk")
    assert last_walk.state == "2026-11-16T17:40:00+00:00"
    assert last_walk.attributes == last_walk.attributes | {
        "start": "2026-11-16T17:00:00+00:00",
        "duration_minutes": 40,
        "vicinity": "Testville",
        "steps": 5000,
        "distance_m": 3200.0,
        "calories": 120,
    }
    walks_call = next(c for c in aioclient_mock.mock_calls if "tracking/day" in str(c[1]))
    assert walks_call[1].query["date"] == "2026-11-16"


async def test_walks_and_charging_unavailable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor(0), walks=None, needs_charging=None)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.rex_needs_charging").state == STATE_UNKNOWN
    assert hass.states.get("sensor.rex_walks_today").state == STATE_UNKNOWN
    assert hass.states.get("sensor.rex_last_walk").state == STATE_UNKNOWN


async def test_network_diagnostics_disabled_by_default(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    for entity_id in ("sensor.rex_network_type", "binary_sensor.rex_rapid_wake_up"):
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC

    data = entry.runtime_data.data[DOG_ID]
    assert _description(SENSORS, "network_protocol").value_fn(data) == "LTE-M"
    assert _description(BINARY_SENSORS, "rapid_wakeup").value_fn(data) is False

    data.monitor["Network"]["Value"]["Protocol"] = 1
    assert _description(SENSORS, "network_protocol").value_fn(data) == "NB-IoT"
    data.monitor["Network"]["Value"]["Protocol"] = 7
    assert _description(SENSORS, "network_protocol").value_fn(data) == "Unknown (7)"

    quality = _description(SENSORS, "network_quality").value_fn
    for value, expected in (
        (1, "minimal"),
        (2, "poor"),
        (3, "fair"),
        (4, "good"),
        (5, "great"),
        (0, "pending"),
        (6, None),
    ):
        data.monitor["Network"]["Value"]["Quality"] = value
        assert quality(data) == expected


def _description(descriptions, key):
    return next(d for d in descriptions if d.key == key)


async def test_total_distance_without_activity_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor(0))
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json=_monitor(0),
    )
    aioclient_mock.get(ACTIVITY_URL, json=ACTIVITY_RESPONSE)
    aioclient_mock.get(COMBINED_URL, json=COMBINED_RESPONSE)
    aioclient_mock.get(
        TOTAL_DISTANCE_URL, json={"DistanceInMeters": 0.0, "HasActivityData": False}
    )
    _mock_location_extras(aioclient_mock)
    aioclient_mock.get(NOMINATIM_URL, json=NOMINATIM_RESPONSE)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.rex_total_distance").state == STATE_UNKNOWN
    # Steps are unaffected.
    assert hass.states.get("sensor.rex_total_steps").state == "1012000"


async def test_total_distance_failure_keeps_detailed_activity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor(0))
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json=_monitor(0),
    )
    aioclient_mock.get(ACTIVITY_URL, json=ACTIVITY_RESPONSE)
    aioclient_mock.get(COMBINED_URL, json=COMBINED_RESPONSE)
    aioclient_mock.get(TOTAL_DISTANCE_URL, status=500)
    _mock_location_extras(aioclient_mock)
    aioclient_mock.get(NOMINATIM_URL, json=NOMINATIM_RESPONSE)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.rex_total_distance").state == STATE_UNKNOWN
    assert hass.states.get("sensor.rex_steps_today").state == "12000"


def _nominatim_calls(aioclient_mock: AiohttpClientMocker) -> list:
    return [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(NOMINATIM_URL)]


async def test_geocoded_location(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(2))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    location = hass.states.get("sensor.rex_geocoded_location")
    assert location.state == "10 Test Road, Testville, AB1 2CD"
    assert location.attributes["full_address"] == NOMINATIM_RESPONSE["display_name"]
    assert location.attributes["street"] == "10 Test Road"
    assert location.attributes["locality"] == "Testville"
    assert location.attributes["postcode"] == "AB1 2CD"
    assert location.attributes["country"] == "United Kingdom"
    assert "OpenStreetMap" in location.attributes["attribution"]
    assert (
        er.async_get(hass).async_get("sensor.rex_geocoded_location").entity_category
        is None
    )

    [call] = _nominatim_calls(aioclient_mock)
    assert call[1].query["lat"] == "51.5"
    assert call[1].query["lon"] == "-0.1"
    assert call[3]["User-Agent"].startswith("HomeAssistant-PitPat/")
    # Nothing identifying the PitPat account goes to OpenStreetMap.
    assert "Authorization" not in call[3]

    # Moving less than 50 m doesn't look the address up again...
    _mock_endpoints(aioclient_mock, _monitor(2, latitude=51.5002))
    await _tick(hass, freezer, 30)
    assert _nominatim_calls(aioclient_mock) == []

    # ...but moving further does.
    _mock_endpoints(
        aioclient_mock,
        _monitor(2, latitude=51.501),
        geocode={"address": {"road": "Other Lane", "town": "Elsewhere"}},
    )
    await _tick(hass, freezer, 30)
    assert len(_nominatim_calls(aioclient_mock)) == 1
    assert hass.states.get("sensor.rex_geocoded_location").state == (
        "Other Lane, Elsewhere"
    )


async def test_geocoded_location_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-11-16T18:15:00+00:00")
    _mock_endpoints(aioclient_mock, _monitor(2))
    aioclient_mock.clear_requests()
    aioclient_mock.get(NOMINATIM_URL, status=503)
    aioclient_mock.post(TOKEN_URL, json=TOKENS)
    aioclient_mock.get(f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs", json=[DOG])
    aioclient_mock.get(
        f"{API_BASE_URL}/api/Users/{USER_ID}/Dogs/{DOG_ID}/Monitors",
        json=_monitor(2),
    )
    aioclient_mock.get(ACTIVITY_URL, json=ACTIVITY_RESPONSE)
    aioclient_mock.get(COMBINED_URL, json=COMBINED_RESPONSE)
    aioclient_mock.get(TOTAL_DISTANCE_URL, json=TOTAL_DISTANCE_RESPONSE)
    _mock_location_extras(aioclient_mock)
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.rex_geocoded_location").state == STATE_UNKNOWN

    # A failed lookup is retried on the next update, even without moving.
    _mock_endpoints(aioclient_mock, _monitor(2), geocode={"error": "Unable to geocode"})
    await _tick(hass, freezer, 30)
    assert len(_nominatim_calls(aioclient_mock)) == 1
    # Nowhere to name (e.g. at sea): unknown, and not looked up again.
    assert hass.states.get("sensor.rex_geocoded_location").state == STATE_UNKNOWN
    await _tick(hass, freezer, 30)
    assert len(_nominatim_calls(aioclient_mock)) == 1


async def test_device_tracker_icon(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_endpoints(aioclient_mock, _monitor(0))
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    tracker = er.async_get(hass).async_get("device_tracker.rex")
    assert tracker.translation_key == "location"
    # Still named after the dog.
    assert hass.states.get("device_tracker.rex").attributes["friendly_name"] == "Rex"
