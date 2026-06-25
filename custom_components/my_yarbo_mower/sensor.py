"""Sensors for My Yarbo Mower."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    APP_NAME,
    DOMAIN,
    HEAD_TYPE_MAP,
    PLANNING_STATUS_MAP,
    RECHARGING_STATUS_MAP,
    RTK_STATUS_MAP,
    UNKNOWN_PLAN,
)
from .coordinator import MyYarboCoordinator
from .entity import MyYarboEntity


@dataclass(frozen=True)
class SensorDef:
    """Sensor definition."""

    key: str
    name: str
    icon: str | None
    path: str | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    mapper: Callable[[Any], Any] | None = None


def _map_int(mapping: dict[int, str], fallback: str = "Unknown"):
    def _mapper(value: Any):
        try:
            raw = int(value)
        except (TypeError, ValueError):
            return None
        return mapping.get(raw, f"{fallback} ({raw})")

    return _mapper


SENSORS = [
    SensorDef("battery", "Battery", "mdi:battery", "BatteryMSG.capacity", PERCENTAGE, SensorDeviceClass.BATTERY),
    SensorDef("rtk_signal", "RTK Signal", "mdi:crosshairs-gps", "RTKMSG.status", None, None, _map_int(RTK_STATUS_MAP, "Weak")),
    SensorDef("gps_satellites", "GPS Satellites", "mdi:satellite-variant", "RTKMSG.sat_num"),
    SensorDef("plan_status", "Plan Status", "mdi:map-clock", "StateMSG.on_going_planning", None, None, _map_int(PLANNING_STATUS_MAP)),
    SensorDef("recharge_status", "Recharge Status", "mdi:battery-charging", "StateMSG.on_going_recharging", None, None, _map_int(RECHARGING_STATUS_MAP)),
    SensorDef("head_type", "Head Type", "mdi:robot-mower", "HeadMsg.head_type", None, None, _map_int(HEAD_TYPE_MAP)),
    SensorDef("error_code", "Error Code", "mdi:alert-circle-outline", "StateMSG.error_code"),
    SensorDef("rain_sensor", "Rain Sensor", "mdi:weather-rainy", "RunningStatusMsg.rain_sensor_data"),
    SensorDef("charging_power", "Charging Power", "mdi:flash", None, UnitOfPower.WATT, SensorDeviceClass.POWER),
]

WEATHER_ENTITY = "weather.forecast_home"
SUN_ENTITY = "sun.sun"
WET_WEATHER = {"rainy", "pouring", "lightning-rainy", "snowy-rainy", "snowy", "hail"}
DAMP_WEATHER = {"fog", "cloudy"}
BAD_WEATHER = {"lightning", "exceptional", "windy", "windy-variant"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator: MyYarboCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        MyYarboSensor(coordinator, device, sensor_def)
        for device in coordinator.devices
        for sensor_def in SENSORS
    ]
    for device in coordinator.devices:
        entities.append(MyYarboConditionSensor(coordinator, device, "mowing_conditions"))
        entities.append(MyYarboConditionSensor(coordinator, device, "grass_wetness"))
        entities.append(MyYarboSequenceSensor(coordinator, device, "plan_sequence"))
        entities.append(MyYarboSequenceSensor(coordinator, device, "previous_completed_plan"))
        entities.append(MyYarboSequenceSensor(coordinator, device, "next_run_plan"))
    async_add_entities(entities)


class MyYarboSensor(MyYarboEntity, SensorEntity):
    """Simple direct Yarbo SDK sensor."""

    def __init__(
        self, coordinator: MyYarboCoordinator, device, sensor_def: SensorDef
    ) -> None:
        super().__init__(coordinator, device, sensor_def.key)
        self._sensor_def = sensor_def
        self._attr_name = f"{APP_NAME} {sensor_def.name}"
        self._attr_icon = sensor_def.icon
        self._attr_native_unit_of_measurement = sensor_def.unit
        self._attr_device_class = sensor_def.device_class

    @property
    def native_value(self):
        """Return sensor value."""
        if self._sensor_def.key == "charging_power":
            voltage = self.float_field("BatteryMSG.voltage")
            current = self.float_field("BatteryMSG.current")
            if voltage is None or current is None:
                return None
            return round(abs(voltage * current), 1)
        value = self.field(self._sensor_def.path) if self._sensor_def.path else None
        if self._sensor_def.mapper:
            return self._sensor_def.mapper(value)
        return value


class MyYarboSequenceSensor(MyYarboEntity, SensorEntity):
    """Local plan sequence status sensor."""

    def __init__(self, coordinator: MyYarboCoordinator, device, key: str) -> None:
        super().__init__(coordinator, device, key)
        self._key = key
        if key == "plan_sequence":
            self._attr_name = f"{APP_NAME} Plan Sequence"
            self._attr_icon = "mdi:playlist-play"
        elif key == "previous_completed_plan":
            self._attr_name = f"{APP_NAME} Previous Completed Plan"
            self._attr_icon = "mdi:check-circle-outline"
        else:
            self._attr_name = f"{APP_NAME} Next Run Plan"
            self._attr_icon = "mdi:skip-next-circle-outline"

    @property
    def native_value(self) -> str:
        """Return sequence status."""
        sn = self._device.sn
        sequence = self.coordinator.plan_sequence.get(sn, [])
        if self._key == "plan_sequence":
            if not sequence:
                return "Empty"
            index = self.coordinator.sequence_index.get(sn, 0) % len(sequence)
            return f"{index + 1}/{len(sequence)}: {sequence[index]}"
        if self._key == "previous_completed_plan":
            return self.coordinator.previous_completed_plan.get(sn) or UNKNOWN_PLAN
        return self.coordinator.next_run_plan(sn) or UNKNOWN_PLAN

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return queue details for dashboard display."""
        sn = self._device.sn
        sequence = list(self.coordinator.plan_sequence.get(sn, []))
        next_sequence_plan = self.coordinator.next_sequence_plan(sn)
        selected_plan = self.coordinator.selected_plan_name.get(sn)
        attrs: dict[str, Any] = {
            "plans": sequence,
            "plan_count": len(sequence),
            "sequence_index": self.coordinator.sequence_index.get(sn, 0)
            if sequence
            else None,
            "next_sequence_plan": next_sequence_plan,
            "selected_plan": selected_plan,
            "next_run_plan": self.coordinator.next_run_plan(sn) or UNKNOWN_PLAN,
            "previous_completed_plan": self.coordinator.previous_completed_plan.get(sn)
            or UNKNOWN_PLAN,
            "active_plan": self.coordinator.active_plan_name.get(sn) or UNKNOWN_PLAN,
        }
        if self._key == "next_run_plan":
            attrs["source"] = "sequence" if next_sequence_plan else "selected_plan"
        return attrs


class MyYarboConditionSensor(MyYarboEntity, SensorEntity):
    """Weather/sun derived mowing condition sensor."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: MyYarboCoordinator, device, key: str) -> None:
        super().__init__(coordinator, device, key)
        self._key = key
        if key == "mowing_conditions":
            self._attr_name = f"{APP_NAME} Mowing Conditions"
            self._attr_icon = "mdi:grass"
        else:
            self._attr_name = f"{APP_NAME} Grass Wetness"
            self._attr_icon = "mdi:water-percent"

    async def async_added_to_hass(self) -> None:
        """Track weather and sun updates directly."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._weather_entity_id(), SUN_ENTITY],
                self._async_source_changed,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_timer_update,
                timedelta(minutes=5),
            )
        )

    @property
    def native_value(self) -> int:
        """Return the computed score."""
        metrics = self._metrics()
        if self._key == "mowing_conditions":
            return metrics["mowing_conditions"]
        return metrics["grass_wetness"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return weather inputs and scoring details."""
        metrics = self._metrics()
        return {
            "weather_entity": metrics["weather_entity"],
            "weather_condition": metrics["weather_condition"],
            "temperature_f": metrics["temperature_f"],
            "humidity": metrics["humidity"],
            "dew_point_f": metrics["dew_point_f"],
            "dew_point_spread_f": metrics["dew_point_spread_f"],
            "wind_speed": metrics["wind_speed"],
            "cloud_coverage": metrics["cloud_coverage"],
            "sun_state": metrics["sun_state"],
            "sun_elevation": metrics["sun_elevation"],
            "after_sunrise_blackout_hours": metrics["after_sunrise_blackout_hours"],
            "before_sunset_blackout_hours": metrics["before_sunset_blackout_hours"],
            "blackout_active": metrics["blackout_active"],
            "blackout_reason": metrics["blackout_reason"],
            "blackout_until": metrics["blackout_until"],
            "mowing_conditions": metrics["mowing_conditions"],
            "grass_wetness": metrics["grass_wetness"],
            "summary": metrics["summary"],
            "details": metrics["details"],
        }

    @callback
    def _async_source_changed(self, _event: Event) -> None:
        self.async_write_ha_state()

    async def _async_timer_update(self, _now) -> None:
        self.async_write_ha_state()

    def _weather_entity_id(self) -> str:
        if self.hass.states.get(WEATHER_ENTITY) is not None:
            return WEATHER_ENTITY
        weather_states = self.hass.states.async_all("weather")
        if weather_states:
            return weather_states[0].entity_id
        return WEATHER_ENTITY

    def _metrics(self) -> dict[str, Any]:
        weather_entity = self._weather_entity_id()
        weather = self.hass.states.get(weather_entity)
        sun = self.hass.states.get(SUN_ENTITY)

        condition = weather.state if weather is not None else None
        attrs = weather.attributes if weather is not None else {}
        sun_attrs = sun.attributes if sun is not None else {}

        temperature_f = self._temperature_f(attrs.get("temperature"))
        dew_point_f = self._temperature_f(attrs.get("dew_point"))
        humidity = self._float_attr(attrs, "humidity")
        wind_speed = self._float_attr(attrs, "wind_speed")
        cloud_coverage = self._float_attr(attrs, "cloud_coverage")
        sun_elevation = self._float_value(sun_attrs.get("elevation"))
        sun_state = sun.state if sun is not None else None
        blackout = self._blackout_status(sun_attrs)

        dew_spread = None
        if temperature_f is not None and dew_point_f is not None:
            dew_spread = round(temperature_f - dew_point_f, 1)

        wetness, wet_reasons = self._grass_wetness(
            condition,
            temperature_f,
            dew_point_f,
            dew_spread,
            humidity,
            wind_speed,
            cloud_coverage,
            sun_state,
            sun_elevation,
        )
        mowing, mowing_reasons = self._mowing_conditions(
            condition,
            temperature_f,
            humidity,
            wind_speed,
            sun_state,
            sun_elevation,
            wetness,
            blackout["active"],
            blackout["reason"],
        )

        summary = self._summary(mowing, wetness)
        return {
            "weather_entity": weather_entity,
            "weather_condition": condition,
            "temperature_f": temperature_f,
            "humidity": humidity,
            "dew_point_f": dew_point_f,
            "dew_point_spread_f": dew_spread,
            "wind_speed": wind_speed,
            "cloud_coverage": cloud_coverage,
            "sun_state": sun_state,
            "sun_elevation": sun_elevation,
            "after_sunrise_blackout_hours": blackout["morning_hours"],
            "before_sunset_blackout_hours": blackout["evening_hours"],
            "blackout_active": blackout["active"],
            "blackout_reason": blackout["reason"],
            "blackout_until": blackout["until"],
            "mowing_conditions": mowing,
            "grass_wetness": wetness,
            "summary": summary,
            "details": ", ".join(mowing_reasons + wet_reasons) or "nominal",
        }

    def _grass_wetness(
        self,
        condition: str | None,
        temperature_f: float | None,
        dew_point_f: float | None,
        dew_spread: float | None,
        humidity: float | None,
        wind_speed: float | None,
        cloud_coverage: float | None,
        sun_state: str | None,
        sun_elevation: float | None,
    ) -> tuple[int, list[str]]:
        wetness = 20
        reasons: list[str] = []

        if condition in WET_WEATHER:
            wetness += 70
            reasons.append("active precipitation")
        elif condition in DAMP_WEATHER:
            wetness += 15
            reasons.append(condition)

        if humidity is not None:
            if humidity >= 95:
                wetness += 35
                reasons.append("very humid")
            elif humidity >= 85:
                wetness += 20
                reasons.append("humid")

        if dew_spread is not None:
            if dew_spread <= 2:
                wetness += 35
                reasons.append("dew likely")
            elif dew_spread <= 5:
                wetness += 20
                reasons.append("dew possible")

        if sun_state == "below_horizon":
            wetness += 20
            reasons.append("no sun drying")
        elif sun_elevation is not None:
            if sun_elevation >= 35:
                wetness -= 20
                reasons.append("sun drying")
            elif sun_elevation >= 10:
                wetness -= 10

        if temperature_f is not None:
            if temperature_f <= 40:
                wetness += 10
            elif temperature_f >= 75 and sun_state == "above_horizon":
                wetness -= 10

        if wind_speed is not None:
            if wind_speed >= 18:
                wetness -= 15
                reasons.append("wind drying")
            elif wind_speed >= 10:
                wetness -= 8

        if cloud_coverage is not None:
            if cloud_coverage >= 80:
                wetness += 10
            elif cloud_coverage <= 30:
                wetness -= 8

        if condition in {"sunny", "clear-night"} and sun_state == "above_horizon":
            wetness -= 8

        return self._clamp(wetness), reasons

    def _mowing_conditions(
        self,
        condition: str | None,
        temperature_f: float | None,
        humidity: float | None,
        wind_speed: float | None,
        sun_state: str | None,
        sun_elevation: float | None,
        wetness: int,
        blackout_active: bool,
        blackout_reason: str | None,
    ) -> tuple[int, list[str]]:
        if blackout_active:
            return 0, [blackout_reason or "sun blackout"]

        score = 100 - int(wetness * 0.45)
        reasons: list[str] = []

        if wetness >= 75:
            reasons.append("grass too wet")
        elif wetness >= 55:
            reasons.append("grass damp")

        if condition in WET_WEATHER:
            score -= 45
            reasons.append("precipitation")
        elif condition in BAD_WEATHER:
            score -= 30
            reasons.append(condition or "bad weather")

        if temperature_f is not None:
            if temperature_f < 45:
                score -= 30
                reasons.append("too cold")
            elif temperature_f < 55:
                score -= 12
                reasons.append("cool")
            elif temperature_f > 95:
                score -= 35
                reasons.append("too hot")
            elif temperature_f > 88:
                score -= 18
                reasons.append("hot")

        if humidity is not None and humidity >= 95:
            score -= 10
            reasons.append("saturated air")

        if wind_speed is not None:
            if wind_speed >= 25:
                score -= 25
                reasons.append("very windy")
            elif wind_speed >= 18:
                score -= 12
                reasons.append("windy")

        if sun_state == "below_horizon":
            score -= 25
            reasons.append("dark")
        elif sun_elevation is not None and sun_elevation < 8:
            score -= 12
            reasons.append("low sun")
        elif (
            sun_elevation is not None
            and sun_elevation > 60
            and temperature_f is not None
            and temperature_f > 85
        ):
            score -= 10
            reasons.append("direct hot sun")

        return self._clamp(score), reasons

    def _blackout_status(self, sun_attrs: dict[str, Any]) -> dict[str, Any]:
        morning_hours = self.coordinator.morning_blackout_hours.get(self._device.sn, 3.0)
        evening_hours = self.coordinator.evening_blackout_hours.get(self._device.sn, 3.0)
        now = dt_util.now()
        next_rising = self._parse_datetime(sun_attrs.get("next_rising"))
        next_setting = self._parse_datetime(sun_attrs.get("next_setting"))

        active = False
        reason = None
        until = None

        if morning_hours > 0 and next_rising is not None:
            last_rising = next_rising
            if last_rising > now:
                last_rising -= timedelta(days=1)
            morning_until = last_rising + timedelta(hours=morning_hours)
            if last_rising <= now < morning_until:
                active = True
                reason = f"sunrise blackout ({morning_hours:g}h)"
                until = morning_until.isoformat()

        if evening_hours > 0 and next_setting is not None:
            evening_start = next_setting - timedelta(hours=evening_hours)
            if evening_start <= now < next_setting:
                active = True
                reason = f"sunset blackout ({evening_hours:g}h)"
                until = next_setting.isoformat()

        return {
            "morning_hours": morning_hours,
            "evening_hours": evening_hours,
            "active": active,
            "reason": reason,
            "until": until,
        }

    def _parse_datetime(self, value: Any):
        if value is None:
            return None
        if hasattr(value, "tzinfo"):
            parsed = value
        else:
            parsed = dt_util.parse_datetime(str(value))
        if parsed is None:
            return None
        return dt_util.as_local(parsed)

    def _summary(self, mowing: int, wetness: int) -> str:
        if mowing >= 80 and wetness <= 35:
            return "excellent"
        if mowing >= 65 and wetness <= 50:
            return "good"
        if mowing >= 45:
            return "marginal"
        return "poor"

    def _temperature_f(self, value: Any) -> float | None:
        raw = self._float_value(value)
        if raw is None:
            return None
        unit = str(self.hass.config.units.temperature_unit)
        if unit in {"°C", "C"}:
            return round(raw * 9 / 5 + 32, 1)
        return round(raw, 1)

    def _float_attr(self, attrs: dict[str, Any], key: str) -> float | None:
        return self._float_value(attrs.get(key))

    def _float_value(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _clamp(self, value: float) -> int:
        return max(0, min(100, int(round(value))))
