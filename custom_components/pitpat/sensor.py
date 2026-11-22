"""Sensors for PitPat dogs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.location import distance

from .api import parse_datetime, unwrap
from .auth import PitPatConnectionError
from .coordinator import (
    LIVE_TRACKING_FIND,
    LIVE_TRACKING_NONE,
    LIVE_TRACKING_WALK,
    PitPatConfigEntry,
    PitPatCoordinator,
    PitPatDogData,
)
from .entity import PitPatEntity, async_add_dog_entities
from .geocode import ATTRIBUTION, async_reverse_geocode

_LOGGER = logging.getLogger(__name__)

# Only look up a new address once the dog has moved this far (metres).
GEOCODE_MIN_DISTANCE = 50

type StateType = str | int | float | datetime | None


def _daily_goal(data: PitPatDogData) -> int | None:
    if goal := (data.today or {}).get("ExerciseGoal"):
        return goal
    # OverridenGoal (sic) is 0 unless the owner has set a custom goal.
    if goal := data.dog.get("OverridenGoal"):
        return goal
    return (data.dog.get("Breed") or {}).get("DailyExerciseMinutes")


def _goal_progress(data: PitPatDogData) -> int | None:
    minutes = (data.today or {}).get("ExerciseMinutes")
    goal = _daily_goal(data)
    if minutes is None or not goal:
        return None
    return round(minutes / goal * 100)


def _detail(key: str) -> Callable[[PitPatDogData], Any]:
    def value(data: PitPatDogData) -> Any:
        return (data.today_detail or {}).get(key)

    return value


def _battery(data: PitPatDogData) -> int | None:
    fraction = (data.monitor_value("BatteryInfo") or {}).get("BatteryLevelFraction")
    return None if fraction is None else round(fraction * 100)


def _contact_time(key: str) -> Callable[[PitPatDogData], datetime | None]:
    def value(data: PitPatDogData) -> datetime | None:
        return parse_datetime((data.monitor_value("ContactTimings") or {}).get(key))

    return value


def _last_sync(data: PitPatDogData) -> datetime | None:
    return _contact_time("LastMessageReceivedAt")(data) or parse_datetime(
        data.dog.get("LastSyncTime")
    )


# Network Protocol values, named as the app shows them.
NETWORK_PROTOCOLS = {0: "LTE-M", 1: "NB-IoT"}


def _network_protocol(data: PitPatDogData) -> str | None:
    protocol = _network("Protocol")(data)
    if protocol is None:
        return None
    return NETWORK_PROTOCOLS.get(protocol, f"Unknown ({protocol})")


TRACKING_MODES = {
    LIVE_TRACKING_NONE: "off",
    LIVE_TRACKING_FIND: "live_tracking",
    LIVE_TRACKING_WALK: "walk",
}

# GpsSynchronisationState values: the steps the app shows after tracking starts.
TRACKING_STATES = {
    0: "idle",
    1: "waiting_for_contact",
    2: "waiting_for_satellites",
    3: "tracking",
}


def _tracking_status(data: PitPatDogData) -> str | None:
    return TRACKING_STATES.get(data.monitor_value("GpsSynchronisationState"))


# Network Quality values, translated as the app names them. The app shows 0
# as pending ("Your PitPat will update this soon").
NETWORK_QUALITIES = {
    0: "pending",
    1: "minimal",
    2: "poor",
    3: "fair",
    4: "good",
    5: "great",
}


def _network_quality(data: PitPatDogData) -> str | None:
    return NETWORK_QUALITIES.get(_network("Quality")(data))


def _network(key: str) -> Callable[[PitPatDogData], Any]:
    def value(data: PitPatDogData) -> Any:
        return unwrap((data.monitor_value("Network") or {}).get(key))

    return value


def _last_walk(data: PitPatDogData) -> dict[str, Any] | None:
    return data.walks[-1] if data.walks else None


def _last_walk_attributes(data: PitPatDogData) -> dict[str, Any] | None:
    if (walk := _last_walk(data)) is None:
        return None
    start = parse_datetime(walk.get("Start"))
    end = parse_datetime(walk.get("End"))
    summary = unwrap(walk.get("Summary")) or {}
    activity = unwrap(summary.get("Activity")) or {}
    return {
        "start": start.isoformat() if start else None,
        "duration_minutes": round((end - start).total_seconds() / 60)
        if start and end
        else None,
        "vicinity": summary.get("Vicinity"),
        "steps": activity.get("TotalSteps"),
        "distance_m": _metres(activity.get("TotalDistance")),
        "calories": activity.get("TotalKCalBurned"),
    }


def _metres(distance: Any) -> float | None:
    """Read a distance, which may be a number or an object of units."""
    if isinstance(distance, int | float):
        return distance
    if isinstance(distance, dict):
        for key in ("Metres", "Meters", "metres", "meters", "Value"):
            if isinstance(distance.get(key), int | float):
                return distance[key]
    return None


@dataclass(frozen=True, kw_only=True)
class PitPatSensorDescription(SensorEntityDescription):
    """Describes a PitPat dog sensor."""

    value_fn: Callable[[PitPatDogData], StateType | datetime]
    attributes_fn: Callable[[PitPatDogData], dict[str, Any] | None] | None = None


SENSORS: tuple[PitPatSensorDescription, ...] = (
    PitPatSensorDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        # WeightUnit 0 appears to be kilograms; the value is always sent in kg.
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        value_fn=lambda data: data.dog.get("Weight") or None,
    ),
    PitPatSensorDescription(
        key="daily_goal",
        translation_key="daily_goal",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_daily_goal,
    ),
    PitPatSensorDescription(
        key="exercise_today",
        translation_key="exercise_today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda data: (data.today or {}).get("ExerciseMinutes"),
    ),
    PitPatSensorDescription(
        key="pottering_today",
        translation_key="pottering_today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda data: (data.today or {}).get("PotteringMinutes"),
    ),
    PitPatSensorDescription(
        key="resting_today",
        translation_key="resting_today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda data: (data.today or {}).get("RestingMinutes"),
    ),
    PitPatSensorDescription(
        key="walking_today",
        translation_key="walking_today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_detail("TotalWalkMinutes"),
    ),
    PitPatSensorDescription(
        key="running_today",
        translation_key="running_today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_detail("TotalRunMinutes"),
    ),
    PitPatSensorDescription(
        key="playing_today",
        translation_key="playing_today",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=_detail("TotalPlayMinutes"),
    ),
    PitPatSensorDescription(
        key="steps_today",
        translation_key="steps_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="steps",
        value_fn=_detail("TotalSteps"),
    ),
    PitPatSensorDescription(
        key="distance_today",
        translation_key="distance_today",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2,
        value_fn=_detail("TotalDistance"),
    ),
    PitPatSensorDescription(
        key="calories_today",
        translation_key="calories_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        suggested_display_precision=0,
        value_fn=_detail("TotalCalories"),
    ),
    # Lifetime count from the monitor; only ever increases, so it suits
    # long-term statistics better than the daily totals.
    PitPatSensorDescription(
        key="total_steps",
        translation_key="total_steps",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="steps",
        value_fn=lambda data: data.total_steps,
    ),
    PitPatSensorDescription(
        key="total_distance",
        translation_key="total_distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=1,
        value_fn=lambda data: data.total_distance,
    ),
    PitPatSensorDescription(
        key="activity_data_until",
        translation_key="activity_data_until",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: parse_datetime(
            (data.today_detail or {}).get("LatestDataTime")
        ),
    ),
    PitPatSensorDescription(
        key="goal_progress",
        translation_key="goal_progress",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_goal_progress,
    ),
    # Yesterday's totals are final, unlike today's last reading before midnight.
    PitPatSensorDescription(
        key="exercise_yesterday",
        translation_key="exercise_yesterday",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda data: (data.yesterday or {}).get("ExerciseMinutes"),
    ),
    PitPatSensorDescription(
        key="goals_met_this_month",
        translation_key="goals_met_this_month",
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement="days",
        value_fn=lambda data: (data.month or {}).get("ExerciseGoalsMet"),
    ),
    PitPatSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=_battery,
    ),
    PitPatSensorDescription(
        key="last_activity",
        translation_key="last_activity",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: parse_datetime(data.monitor_value("LastActivityTime")),
    ),
    PitPatSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_sync,
    ),
    PitPatSensorDescription(
        key="next_contact",
        translation_key="next_contact",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_contact_time("NextMessageExpectedAt"),
    ),
    PitPatSensorDescription(
        key="location_time",
        translation_key="location_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: parse_datetime(
            (data.monitor_value("LastKnownPosition") or {}).get("DataTime")
        ),
    ),
    PitPatSensorDescription(
        key="location_accuracy",
        translation_key="location_accuracy",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=0,
        value_fn=lambda data: (
            (data.monitor_value("LastKnownPosition") or {}).get("Accuracy") or {}
        ).get("Metres"),
    ),
    PitPatSensorDescription(
        key="walks_today",
        translation_key="walks_today",
        value_fn=lambda data: None if data.walks is None else len(data.walks),
    ),
    # When today's latest walk ended, with its details as attributes.
    PitPatSensorDescription(
        key="last_walk",
        translation_key="last_walk",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: parse_datetime((_last_walk(data) or {}).get("End")),
        attributes_fn=_last_walk_attributes,
    ),
    PitPatSensorDescription(
        key="tracking_mode",
        translation_key="tracking_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(TRACKING_MODES.values()),
        value_fn=lambda data: TRACKING_MODES.get(
            data.monitor_value("LiveTrackingReason")
        ),
    ),
    PitPatSensorDescription(
        key="tracking_status",
        translation_key="tracking_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(TRACKING_STATES.values()),
        value_fn=_tracking_status,
    ),
    # 0 to 4, like signal bars. The app never shows it.
    PitPatSensorDescription(
        key="signal_strength",
        translation_key="signal_strength",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.monitor_value("SignalStrength"),
    ),
    PitPatSensorDescription(
        key="network_quality",
        translation_key="network_quality",
        device_class=SensorDeviceClass.ENUM,
        options=list(NETWORK_QUALITIES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_network_quality,
    ),
    PitPatSensorDescription(
        key="network_operator",
        translation_key="network_operator",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_network("NetworkOperator"),
    ),
    PitPatSensorDescription(
        key="network_protocol",
        translation_key="network_protocol",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_network_protocol,
    ),
)


GEOCODED_LOCATION = SensorEntityDescription(
    key="geocoded_location",
    translation_key="geocoded_location",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PitPatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    async_add_dog_entities(
        entry,
        async_add_entities,
        lambda coordinator, dog_id: [
            *(
                PitPatSensor(coordinator, dog_id, description)
                for description in SENSORS
            ),
            PitPatGeocodedLocationSensor(coordinator, dog_id, GEOCODED_LOCATION),
        ],
    )


class PitPatSensor(PitPatEntity, SensorEntity):
    """A sensor for one attribute of a dog."""

    entity_description: PitPatSensorDescription

    @property
    def native_value(self) -> StateType | datetime:
        return self.entity_description.value_fn(self.dog_data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.dog_data)


class PitPatGeocodedLocationSensor(PitPatEntity, SensorEntity):
    """The address at the dog's last known position, from OpenStreetMap."""

    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: PitPatCoordinator,
        dog_id: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, dog_id, description)
        self._attr_extra_state_attributes = {}
        # Where the current address was looked up, and any lookup in progress.
        self._geocoded_at: tuple[float, float] | None = None
        self._lookup: asyncio.Task[None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._async_geocode_if_moved()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_geocode_if_moved()
        super()._handle_coordinator_update()

    @callback
    def _async_geocode_if_moved(self) -> None:
        if self._dog_id not in self.coordinator.data:
            return
        position = self.dog_data.monitor_value("LastKnownPosition") or {}
        latitude, longitude = position.get("Latitude"), position.get("Longitude")
        if latitude is None or longitude is None:
            return
        if self._lookup is not None and not self._lookup.done():
            return
        if (
            self._geocoded_at is not None
            and (distance(*self._geocoded_at, latitude, longitude) or 0)
            < GEOCODE_MIN_DISTANCE
        ):
            return
        self._lookup = self.coordinator.config_entry.async_create_background_task(
            self.hass,
            self._async_geocode(latitude, longitude),
            "pitpat reverse geocode",
        )

    async def _async_geocode(self, latitude: float, longitude: float) -> None:
        try:
            place = await async_reverse_geocode(
                async_get_clientsession(self.hass),
                latitude,
                longitude,
                self.hass.config.language,
            )
        except PitPatConnectionError as err:
            # Tried again on the next update.
            _LOGGER.debug("Could not look up address: %s", err)
            return
        self._geocoded_at = (latitude, longitude)
        self._attr_native_value = place.name[:255] if place else None
        self._attr_extra_state_attributes = place.attributes if place else {}
        self.async_write_ha_state()
