"""Fixtures for PitPat tests."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.ha_pitpat.auth import TokenSet

USER_ID = "d1f49674-a6f4-4719-80e9-e87ccdda1507"
DOG_ID = "1678dc30-823f-48ed-a890-0840ed6e63f2"

DOG = {
    "Name": "Rex",
    "Breed": {"Name": "Test Breed", "DailyExerciseMinutes": 60},
    "Monitor": {
        "BatteryVoltage": None,
        "FirmwareVersion": "1.4.4",
        "HardwareVersion": "6.3.5",
        "Model": 6,
    },
    "Weight": 12.5,
    "WeightUnit": 0,
    "OverridenGoal": 0,
    "LastSyncTime": "0001-01-01T00:00:00Z",
    "IanaTimeZoneName": "Europe/London",
    "Id": DOG_ID,
}


def activity_day(day: str, exercise: int, goal_met: bool) -> dict:
    return {
        "HasMetExerciseGoal": goal_met,
        "ExerciseGoal": 60,
        "ExerciseMinutes": exercise,
        "PotteringMinutes": 1,
        "RestingMinutes": 1051,
        "Date": f"{day}T00:00:00Z",
        "HasRecording": False,
    }


def step_block(time: str, cumulative: int) -> dict:
    return {
        "DogId": DOG_ID,
        "DataTime": f"2026-11-16T{time}:00Z",
        "CumulativeStepCount": cumulative,
    }


COMBINED_RESPONSE = {
    "ActivityDay": {
        "DogId": DOG_ID,
        "Date": "2026-11-16T00:00:00Z",
        "TotalWalkMinutes": 21,
        "TotalRunMinutes": 46,
        "TotalPlayMinutes": 7,
        "TotalPotteringMinutes": 18,
        "TotalRestMinutes": 1098,
        "TotalCalories": 340,
        "TotalSteps": 12000,
        "TotalDistance": 5000.0,
        "EarliestDataTime": "2026-11-16T00:00:00Z",
        "LatestDataTime": "2026-11-16T17:45:00Z",
    },
    "StepBlocksForToday": [
        step_block("00:00", 1000000),
        step_block("17:45", 1012000),
    ],
    "StepBlocksForNextDay": [],
    "ActivityBlocks": [],
    "NeedsFetch": False,
}


ACTIVITY_RESPONSE = [
    {
        "FormattedDate": "November 2026",
        "ExerciseGoalsMet": 11,
        "Date": "2026-11-01T00:00:00Z",
        "ActivityDays": [
            activity_day("2026-11-16", 43, False),
            activity_day("2026-11-15", 62, True),
        ],
    }
]


MONITOR_RESPONSE = {
    "HasValue": True,
    "Value": {
        "DogId": DOG_ID,
        "Monitor": {
            "SerialNumber": "TESTSERIAL",
            "BatteryInfo": {
                "HasValue": True,
                "Value": {"IsCharging": False, "BatteryLevelFraction": 0.9285714285714286},
            },
            "LastKnownPosition": {
                "HasValue": True,
                "Value": {
                    "Latitude": 51.5,
                    "Longitude": -0.1,
                    "Accuracy": {"Metres": 14.0},
                    "DataTime": "2026-11-16T13:09:35Z",
                },
            },
            "ContactTimings": {
                "HasValue": True,
                "Value": {
                    "NextMessageExpectedAt": "2026-11-16T18:33:38Z",
                    "LastMessageReceivedAt": "2026-11-16T18:13:38.7647155Z",
                    "LastMessageSentAt": "2026-11-16T18:13:38Z",
                },
            },
            "SignalStrength": 1,
            "LiveTrackingReason": 0,
            "GpsSynchronisationState": 0,
            "PhoneHomeCadence": 0,
            "BackgroundTrackingEnabled": False,
            "LostContactInfo": {"HasValue": False},
            "LastActivityTime": {"HasValue": True, "Value": "2026-11-16T18:10:00Z"},
            "Network": {
                "HasValue": True,
                "Value": {
                    "Quality": 3,
                    "Protocol": 0,
                    "HasRapidWakeup": False,
                    "NetworkOperator": {"HasValue": True, "Value": "3"},
                },
            },
            "Id": "2c22f9b1-fa56-4533-996b-7c586e924bf5",
            "MonitorVersion": {
                "Model": 6,
                "Firmware": {"HasValue": True, "Value": "1.4.4"},
                "Hardware": {"HasValue": True, "Value": "6.3.5"},
            },
            "Type": "Cellular",
        },
    },
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def make_tokens(refresh_token: str = "refresh-1") -> TokenSet:
    return TokenSet("access", refresh_token, time.time() + 3600)


@pytest.fixture
def mock_login():
    with patch(
        "custom_components.ha_pitpat.config_flow.async_login",
        AsyncMock(return_value=make_tokens()),
    ) as mock:
        yield mock


@pytest.fixture
def mock_api():
    """Patch the API client methods used by the flow and coordinator."""
    with (
        patch(
            "custom_components.ha_pitpat.api.PitPatApiClient.async_get_settings",
            AsyncMock(return_value={"UserId": USER_ID, "UserDogTotal": "1"}),
        ) as settings,
        patch(
            "custom_components.ha_pitpat.api.PitPatApiClient.async_get_dogs",
            AsyncMock(return_value=[DOG]),
        ) as dogs,
        patch(
            "custom_components.ha_pitpat.api.async_refresh",
            AsyncMock(return_value=make_tokens("refresh-2")),
        ) as refresh,
    ):
        yield {"settings": settings, "dogs": dogs, "refresh": refresh}


class FakeLiveConnection:
    """Stands in for the SignalR connection in integration tests."""

    def __init__(self, live: "FakeLive", session, client, user_id, on_monitor) -> None:
        self._live = live
        self.on_monitor = on_monitor
        self.dogs: set[str] = set()
        self.closed = False
        live.connections.append(self)

    async def async_connect(self) -> None:
        pass

    async def async_set_dogs(self, dog_ids) -> None:
        self.dogs = set(dog_ids)

    async def async_listen(self) -> None:
        # By default the connection drops at once, so tests waiting for
        # background tasks don't wait forever.
        if self._live.stay_connected:
            await asyncio.Event().wait()

    async def async_close(self) -> None:
        self.closed = True


class FakeLive:
    def __init__(self) -> None:
        self.connections: list[FakeLiveConnection] = []
        self.stay_connected = False


@pytest.fixture(autouse=True)
def no_geocode_spacing():
    with (
        patch("custom_components.ha_pitpat.geocode.MIN_INTERVAL", 0),
        patch("custom_components.ha_pitpat.geocode._last_request", 0.0),
    ):
        yield


@pytest.fixture(autouse=True)
def mock_live():
    live = FakeLive()
    with patch(
        "custom_components.ha_pitpat.coordinator.PitPatLiveConnection",
        lambda *args: FakeLiveConnection(live, *args),
    ):
        yield live
