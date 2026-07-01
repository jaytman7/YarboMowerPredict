"""Coordinator for My Yarbo Mower."""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from yarbo_robot_sdk import (
    AuthenticationError,
    TokenExpiredError,
    YarboClient,
    YarboSDKError,
)
from yarbo_robot_sdk.device_helpers import extract_field

from .const import (
    ACTIVE_PLANNING_STATES,
    APP_NAME,
    AUTO_MAX_WETNESS_DEFAULT,
    AUTO_MIN_BATTERY_DEFAULT,
    AUTO_MIN_FAVORABILITY_DEFAULT,
    AUTO_MIN_GRASS_GROWTH_DEFAULT,
    AUTO_START_GRACE_MINUTES_DEFAULT,
    AUTO_WAKE_INTERVAL_MINUTES_DEFAULT,
    AUTO_WAKE_LEAD_MINUTES_DEFAULT,
    CHARGING_FULL_NOISE_BATTERY_PERCENT,
    CHARGING_RECHARGE_STATE,
    CONF_SELECTED_DEVICES,
    DATA_ACCESS_TOKEN,
    DATA_REFRESH_TOKEN,
    DOMAIN,
    describe_error_code,
    COMPLETED_PLANNING_STATE,
    MOWER_HEAD_TYPES,
    RTK_READY,
    UNKNOWN_PLAN,
)

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_TIMEOUT_SECONDS = 90
HEARTBEAT_CHECK_INTERVAL = timedelta(seconds=5)
SEQUENCE_STORE_VERSION = 1
SEQUENCE_STORE_DELAY = 2
GROWTH_UPDATE_INTERVAL = timedelta(minutes=30)
GROWTH_MAX_CATCHUP_HOURS = 24.0
GROWTH_PROFILES: dict[str, dict[str, Any]] = {
    "cold_weather": {
        "label": "Cold weather grass",
        "model": "cold-weather grass temperature potential",
        "max_daily_inches": 0.16,
        "optimal_temp_f": 68.0,
        "temp_spread_f": 18.0,
        "minimum_temp_f": 38.0,
        "maximum_temp_f": 100.0,
    },
    "warm_weather": {
        "label": "Warm weather grass",
        "model": "warm-weather grass temperature potential",
        "max_daily_inches": 0.18,
        "optimal_temp_f": 86.0,
        "temp_spread_f": 16.0,
        "minimum_temp_f": 50.0,
        "maximum_temp_f": 108.0,
    },
}
WEATHER_LOOKAHEAD_HOURS = 3
WEATHER_LOOKAHEAD_UPDATE_INTERVAL = timedelta(minutes=15)
WEATHER_LOOKAHEAD_PRECIP_PROBABILITY = 35.0
WEATHER_LOOKAHEAD_PRECIP_AMOUNT = 0.01
WEATHER_LOOKAHEAD_WIND_SPEED = 25.0
AUTO_SEQUENCE_CHECK_INTERVAL = timedelta(minutes=1)
AUTO_SEQUENCE_START_RETRY_INTERVAL = timedelta(minutes=10)
BEST_MOW_START_FORECAST_DAYS = 3
BEST_MOW_START_LOOKAHEAD_HOURS = BEST_MOW_START_FORECAST_DAYS * 24
BEST_MOW_START_TARGET_TEMP_F = 68.0
BEST_MOW_START_MIN_DRYING_HOURS = 6.0
BEST_MOW_START_MIN_SCORE = 55.0
WEATHER_ENTITY = "weather.forecast_home"
PREFERRED_WEATHER_PLATFORMS = ("accuweather",)
SUN_ENTITY = "sun.sun"
UNAVAILABLE_STATES = {STATE_UNKNOWN, STATE_UNAVAILABLE}
WET_WEATHER = {"rainy", "pouring", "lightning-rainy", "snowy-rainy", "snowy", "hail"}
START_BLOCK_WEATHER = WET_WEATHER | {"lightning", "exceptional"}
BAD_GROWTH_WEATHER = {"lightning", "exceptional", "hail", "snowy", "snowy-rainy"}
SUNNY_WEATHER = {"sunny", "partlycloudy", "partly-cloudy"}
DULL_WEATHER = {"cloudy", "fog"}


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> bool:
    """Merge source into target without dropping existing nested values."""
    changed = False
    for key, value in source.items():
        if key in ("__online__", "HeartBeatMSG"):
            continue
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if target[key].get(nested_key) != nested_value:
                    target[key][nested_key] = nested_value
                    changed = True
        elif target.get(key) != value:
            target[key] = value
            changed = True
    return changed


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MyYarboCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Owns the direct SDK connection for the standalone app."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        self.data = {}
        self._client: YarboClient | None = None
        self.devices: list[Any] = []
        self.plan_data: dict[str, list[dict[str, Any]]] = {}
        self.selected_plan: dict[str, int | None] = {}
        self.selected_plan_name: dict[str, str | None] = {}
        self.plan_start_percent: dict[str, int] = {}
        self.morning_blackout_hours: dict[str, float] = {}
        self.evening_blackout_hours: dict[str, float] = {}
        self.auto_wake_checks: dict[str, bool] = {}
        self.auto_sequence_start: dict[str, bool] = {}
        self.auto_min_battery: dict[str, float] = {}
        self.auto_min_favorability: dict[str, float] = {}
        self.auto_max_wetness: dict[str, float] = {}
        self.auto_min_grass_growth: dict[str, float] = {}
        self.auto_start_grace_minutes: dict[str, float] = {}
        self.auto_wake_lead_minutes: dict[str, float] = {}
        self.auto_wake_interval_minutes: dict[str, float] = {}
        self._best_mow_start_hold: dict[str, dict[str, Any]] = {}
        self.warm_weather_grass: dict[str, bool] = {}
        self.weather_source: dict[str, str] = {}
        self.plan_sequence: dict[str, list[str]] = {}
        self.sequence_index: dict[str, int] = {}
        self.sequence_picker: dict[str, str | None] = {}
        self.active_plan_name: dict[str, str | None] = {}
        self.active_sequence_plan: dict[str, bool] = {}
        self.previous_completed_plan: dict[str, str | None] = {}
        self.plan_growth_inches: dict[str, dict[str, float]] = {}
        self.plan_growth_started_at: dict[str, dict[str, str]] = {}
        self.plan_last_mowed_at: dict[str, dict[str, str]] = {}
        self._last_growth_update: dict[str, str] = {}
        self.weather_lookahead: dict[str, Any] = {
            "status": "unknown",
            "blocked": False,
            "reason": "not checked",
            "horizon_hours": WEATHER_LOOKAHEAD_HOURS,
        }
        self.weather_forecast: list[dict[str, Any]] = []
        self.auto_sequence_status: dict[str, dict[str, Any]] = {}
        self._last_auto_wake_at: dict[str, str] = {}
        self._last_auto_start_attempt_at: dict[str, str] = {}
        self._last_auto_error: dict[str, str | None] = {}
        self._last_error_code: dict[str, int | None] = {}
        self._last_planning_state: dict[str, int | None] = {}
        self._sequence_store = Store(
            hass, SEQUENCE_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_sequence"
        )
        self._last_heartbeat: dict[str, float] = {}
        self._unsub_heartbeat_check: CALLBACK_TYPE | None = None
        self._unsub_growth_update: CALLBACK_TYPE | None = None
        self._unsub_weather_lookahead: CALLBACK_TYPE | None = None
        self._unsub_weather_state: CALLBACK_TYPE | None = None
        self._unsub_auto_sequence_check: CALLBACK_TYPE | None = None

    async def async_setup(self) -> None:
        """Create SDK client, authenticate, subscribe, and fetch initial data."""
        await self._async_restore_sequence()
        api_url = os.environ.get("YARBO_API_BASE_URL")

        def _create_client() -> YarboClient:
            return YarboClient(api_base_url=api_url) if api_url else YarboClient()

        client = await self.hass.async_add_executor_job(_create_client)
        self._client = client

        email = self.entry.data[CONF_EMAIL]
        token = self.entry.data.get(DATA_ACCESS_TOKEN)
        refresh_token = self.entry.data.get(DATA_REFRESH_TOKEN)

        try:
            if token and refresh_token:
                await self.hass.async_add_executor_job(
                    client.restore_session, email, token, refresh_token
                )
            else:
                await self.hass.async_add_executor_job(
                    client.login, email, self.entry.data[CONF_PASSWORD]
                )
        except (AuthenticationError, TokenExpiredError) as err:
            raise ConfigEntryAuthFailed from err

        await self._async_refresh_stored_tokens()

        try:
            all_devices = await self.hass.async_add_executor_job(client.get_devices)
        except (TokenExpiredError, AuthenticationError) as err:
            raise ConfigEntryAuthFailed from err
        except YarboSDKError as err:
            raise ConfigEntryAuthFailed from err

        selected = set(self.entry.options.get(CONF_SELECTED_DEVICES, []))
        self.devices = [device for device in all_devices if not selected or device.sn in selected]

        try:
            await self.hass.async_add_executor_job(client.mqtt_connect)
            for device in self.devices:
                await self.hass.async_add_executor_job(
                    client.subscribe_device_message,
                    device.sn,
                    device.type_id,
                    self._on_device_status,
                )
                await self.hass.async_add_executor_job(
                    client.subscribe_heart_beat,
                    device.sn,
                    device.type_id,
                    self._on_heart_beat,
                )
                try:
                    await self.hass.async_add_executor_job(
                        client.subscribe_data_feedback,
                        device.sn,
                        device.type_id,
                        None,
                    )
                except YarboSDKError as err:
                    _LOGGER.debug("data_feedback subscription failed for %s: %s", device.sn, err)
        except YarboSDKError as err:
            _LOGGER.warning("Yarbo MQTT setup failed: %s", err)

        self._unsub_heartbeat_check = async_track_time_interval(
            self.hass, self._async_check_heartbeats, HEARTBEAT_CHECK_INTERVAL
        )
        self._unsub_growth_update = async_track_time_interval(
            self.hass, self._async_update_plan_growth, GROWTH_UPDATE_INTERVAL
        )
        self._unsub_weather_lookahead = async_track_time_interval(
            self.hass,
            self._async_update_weather_lookahead,
            WEATHER_LOOKAHEAD_UPDATE_INTERVAL,
        )
        self._unsub_auto_sequence_check = async_track_time_interval(
            self.hass,
            self._async_auto_sequence_check,
            AUTO_SEQUENCE_CHECK_INTERVAL,
        )
        self._refresh_weather_source_listener()

        self.entry.async_create_background_task(
            self.hass,
            self._async_initial_fetch(),
            name=f"{DOMAIN}_initial_fetch",
        )
        self.entry.async_create_background_task(
            self.hass,
            self._async_update_weather_lookahead(),
            name=f"{DOMAIN}_weather_lookahead",
        )
        self.async_set_updated_data(self.data)

    async def async_shutdown(self) -> None:
        """Shut down SDK resources."""
        if self._unsub_heartbeat_check:
            self._unsub_heartbeat_check()
            self._unsub_heartbeat_check = None
        if self._unsub_growth_update:
            self._unsub_growth_update()
            self._unsub_growth_update = None
        if self._unsub_weather_lookahead:
            self._unsub_weather_lookahead()
            self._unsub_weather_lookahead = None
        if self._unsub_auto_sequence_check:
            self._unsub_auto_sequence_check()
            self._unsub_auto_sequence_check = None
        if self._unsub_weather_state:
            self._unsub_weather_state()
            self._unsub_weather_state = None
        if self._client is not None:
            await self.hass.async_add_executor_job(self._client.close)
            self._client = None

    def bound_device(self, sn: str):
        """Return an SDK bound device for a serial number."""
        if self._client is None:
            return None
        device = self.device_by_sn(sn)
        if device is None:
            return None
        return self._client.device(device, data=(self.data or {}).get(sn))

    def device_by_sn(self, sn: str):
        """Find a selected SDK device by serial number."""
        return next((device for device in self.devices if device.sn == sn), None)

    async def async_refresh_all(self, sn: str) -> None:
        """Refresh plan list and device snapshot."""
        device = self.device_by_sn(sn)
        if device is None:
            return
        await self.async_refresh_device_msg(sn, device.type_id)
        await self.async_refresh_plans(sn, device.type_id)

    async def async_refresh_device_msg(self, sn: str, type_id: str) -> None:
        """Refresh a full DeviceMSG snapshot."""
        if self._client is None:
            return
        try:
            bound = self.bound_device(sn)
            if bound is not None:
                result = await self.hass.async_add_executor_job(
                    bound.core.get_device_msg, 20.0
                )
            else:
                result = await self.hass.async_add_executor_job(
                    self._client.get_device_msg, sn, type_id, 20.0
                )
            msg_data = result.get("data", {})
            self.data.setdefault(sn, {})
            _deep_merge(self.data[sn], msg_data)
            self._track_plan_transition(sn)
            self._track_error_code(sn)
            self.async_set_updated_data(self.data)
        except TimeoutError:
            _LOGGER.warning("DeviceMSG request timed out for %s", sn)
        except Exception as err:
            _LOGGER.warning("DeviceMSG request failed for %s: %s", sn, err)

    async def async_refresh_plans(self, sn: str, type_id: str) -> None:
        """Refresh the robot's plan list."""
        if self._client is None:
            return
        try:
            bound = self.bound_device(sn)
            if bound is not None:
                result = await self.hass.async_add_executor_job(bound.core.read_all_plan)
            else:
                result = await self.hass.async_add_executor_job(
                    self._client.read_all_plan, sn, type_id
                )
            self.plan_data[sn] = result.get("data", {}).get("data", [])
            self._ensure_sequence_picker(sn)
            self._ensure_plan_growth_entries(sn)
            self._sort_sequence_by_growth(sn)
            self.sync_selected_plan_to_next(sn)
            self._persist_sequence()
            self.async_set_updated_data(self.data)
        except TimeoutError:
            _LOGGER.warning("Plan request timed out for %s", sn)
        except Exception as err:
            _LOGGER.warning("Plan request failed for %s: %s", sn, err)

    async def async_set_working_state(self, sn: str, state: int) -> None:
        """Set Yarbo working state: 1 working, 0 standby."""
        device = self.device_by_sn(sn)
        if device is None or self._client is None:
            return
        bound = self.bound_device(sn)
        if bound is not None:
            await self.hass.async_add_executor_job(bound.core.set_working_state, state)
            return
        await self.hass.async_add_executor_job(
            self._client.mqtt_publish_command,
            sn,
            device.type_id,
            "set_working_state",
            {"state": state, "source": "smart_home"},
        )

    async def async_start_plan(self, sn: str, *, use_sequence: bool = False) -> None:
        """Start the selected plan, or explicitly start the next queued plan."""
        await self._async_update_weather_lookahead()
        if self.weather_start_blocked():
            raise ValueError(self.weather_start_block_reason())

        device = self.device_by_sn(sn)
        sequence_plan = self.next_sequence_plan(sn) if use_sequence else None
        if use_sequence and sequence_plan is None:
            raise ValueError("No queued Yarbo sequence plan")
        if use_sequence:
            growth = self._plan_growth_value(sn, sequence_plan)
            min_growth = self.auto_min_grass_growth.get(
                sn, AUTO_MIN_GRASS_GROWTH_DEFAULT
            )
            if growth < min_growth:
                raise ValueError(
                    f"Cannot start: {sequence_plan} growth is {growth:.2f} in, "
                    f"below {min_growth:g} in"
                )

        plan_name = sequence_plan or self.selected_plan_name.get(sn)
        plan_id = (
            self.plan_id_by_name(sn, sequence_plan)
            if sequence_plan
            else self.selected_plan.get(sn)
        )
        if plan_id is None and device is not None and self._client is not None:
            await self.async_refresh_plans(sn, device.type_id)
            plan_id = (
                self.plan_id_by_name(sn, sequence_plan)
                if sequence_plan
                else self.selected_plan.get(sn)
            )
        if device is None or self._client is None or plan_id is None:
            raise ValueError("No Yarbo plan selected")
        if plan_name is None:
            plan_name = self.plan_name_by_id(sn, plan_id)
        start_percent = max(0, min(100, int(self.plan_start_percent.get(sn, 0))))
        bound = self.bound_device(sn)
        if bound is not None:
            await self.hass.async_add_executor_job(
                bound.core.start_plan, plan_id, start_percent
            )
            self._mark_plan_started(sn, plan_name, sequence_plan is not None)
            return
        payload = {"id": plan_id}
        if start_percent > 0:
            payload["percent"] = start_percent
        await self.hass.async_add_executor_job(
            self._client.mqtt_publish_command,
            sn,
            device.type_id,
            "start_plan",
            payload,
        )
        self._mark_plan_started(sn, plan_name, sequence_plan is not None)

    def sequence_auto_ready_status(self, sn: str) -> dict[str, Any]:
        """Return automation readiness details for the next queued sequence plan."""
        now = dt_util.now()
        next_plan = self.next_sequence_plan(sn)
        best = self.best_mow_start(sn)
        best_at = self._parse_datetime(best.get("start_at"))
        wake_lead_minutes = self.auto_wake_lead_minutes.get(
            sn, AUTO_WAKE_LEAD_MINUTES_DEFAULT
        )
        wake_interval_minutes = self.auto_wake_interval_minutes.get(
            sn, AUTO_WAKE_INTERVAL_MINUTES_DEFAULT
        )
        start_grace = self._auto_start_grace_delta(sn)
        start_grace_minutes = start_grace.total_seconds() / 60.0
        min_battery = self.auto_min_battery.get(sn, AUTO_MIN_BATTERY_DEFAULT)
        min_favorability = self.auto_min_favorability.get(
            sn, AUTO_MIN_FAVORABILITY_DEFAULT
        )
        max_wetness = self.auto_max_wetness.get(sn, AUTO_MAX_WETNESS_DEFAULT)
        min_growth = self.auto_min_grass_growth.get(
            sn, AUTO_MIN_GRASS_GROWTH_DEFAULT
        )
        next_growth = self._plan_growth_value(sn, next_plan)
        growth_candidate = bool(next_plan and next_growth >= min_growth)

        wake_window_start = (
            best_at - timedelta(minutes=wake_lead_minutes) if best_at else None
        )
        start_window_end = best_at + start_grace if best_at else None
        in_wake_window = bool(
            best_at
            and wake_window_start is not None
            and start_window_end is not None
            and wake_window_start <= now <= start_window_end
        )
        in_start_window = bool(
            best_at and start_window_end is not None and best_at <= now <= start_window_end
        )

        checks: list[dict[str, Any]] = []
        reasons: list[str] = []

        def add_check(name: str, passed: bool, reason: str) -> None:
            checks.append(
                {
                    "name": name,
                    "passed": passed,
                    "reason": None if passed else reason,
                }
            )
            if not passed:
                reasons.append(reason)

        add_check("sequence", bool(next_plan), "no queued sequence plan")
        add_check(
            "minimum_growth",
            not next_plan or growth_candidate,
            f"next sequence growth {next_growth:.2f} in below {min_growth:g} in",
        )
        add_check(
            "best_start",
            best_at is not None and best.get("status") == "ready",
            best.get("reason") or "best start unavailable",
        )
        add_check(
            "start_window",
            in_start_window,
            "outside best-start grace window",
        )

        weather_clear = (
            self.weather_lookahead.get("status") == "clear"
            and not self.weather_lookahead.get("blocked")
        )
        add_check(
            "weather_window",
            weather_clear,
            str(self.weather_lookahead.get("reason") or "weather window unknown"),
        )

        favorability = self._entity_state_float(sn, "sensor", "mowing_conditions")
        wetness = self._entity_state_float(sn, "sensor", "grass_wetness")
        add_check(
            "mowing_favorability",
            favorability is not None and favorability >= min_favorability,
            f"favorability below {min_favorability:g}%",
        )
        add_check(
            "grass_wetness",
            wetness is not None and wetness <= max_wetness,
            f"wetness above {max_wetness:g}%",
        )

        data = (self.data or {}).get(sn, {})
        online = data.get("__online__") is True
        add_check("online", online, "mower is not online")

        battery = self._field_float(sn, "BatteryMSG.capacity")
        add_check(
            "battery",
            battery is not None and battery >= min_battery,
            f"battery below {min_battery:g}%",
        )

        rtk = self._field_int(sn, "RTKMSG.status")
        add_check("rtk", rtk in RTK_READY, "RTK is not ready")

        head_type = self._field_int(sn, "HeadMsg.head_type")
        add_check(
            "mower_head",
            head_type in MOWER_HEAD_TYPES,
            "mower head is not attached",
        )

        charging_status = self._field_int(sn, "BatteryMSG.status")
        charging = self.battery_charging(sn)
        add_check(
            "charging",
            charging is False,
            "charging status unavailable" if charging is None else "mower is charging",
        )

        wired_charging = self._field_int(sn, "BodyMsg.rechargeState")
        add_check(
            "wired_charging",
            wired_charging not in (1, 3),
            "mower is wired charging",
        )

        planning = self._planning_state(sn)
        add_check(
            "planning_state",
            planning is None or planning <= 0 or planning == COMPLETED_PLANNING_STATE,
            "mower is already running a plan",
        )

        recharging = self._field_int(sn, "StateMSG.on_going_recharging")
        add_check(
            "returning_state",
            recharging in (None, 0, CHARGING_RECHARGE_STATE),
            "mower is returning",
        )

        error_code = self._field_int(sn, "StateMSG.error_code")
        error_description = describe_error_code(error_code)
        add_check(
            "error_code",
            error_code in (None, 0),
            f"mower error code {error_code}: {error_description}",
        )

        obstacle = self._field_int(sn, "StateMSG.obstacle")
        add_check("obstacle", obstacle in (None, 0), "obstacle is active")

        stuck = self._field_int(sn, "StateMSG.stuck")
        add_check("stuck", stuck in (None, 0), "mower is stuck")

        ready = not reasons
        wake_interval_due = self._minutes_elapsed_since(
            self._last_auto_wake_at.get(sn),
            wake_interval_minutes,
            now,
        )
        start_retry_due = self._minutes_elapsed_since(
            self._last_auto_start_attempt_at.get(sn),
            AUTO_SEQUENCE_START_RETRY_INTERVAL.total_seconds() / 60,
            now,
        )
        wake_due = bool(
            self.auto_wake_checks.get(sn, False)
            and next_plan
            and growth_candidate
            and in_wake_window
            and wake_interval_due
            and not self._mower_active_or_returning(sn)
        )
        start_due = bool(
            self.auto_sequence_start.get(sn, False)
            and ready
            and start_retry_due
        )

        return {
            "ready": ready,
            "reason": "ready" if ready else reasons[0],
            "reasons": reasons,
            "checks": checks,
            "auto_wake_enabled": self.auto_wake_checks.get(sn, False),
            "auto_start_enabled": self.auto_sequence_start.get(sn, False),
            "wake_due": wake_due,
            "start_due": start_due,
            "in_wake_window": in_wake_window,
            "in_start_window": in_start_window,
            "wake_interval_due": wake_interval_due,
            "start_retry_due": start_retry_due,
            "checked_at": now.isoformat(),
            "next_sequence_plan": next_plan or UNKNOWN_PLAN,
            "best_start_at": best_at.isoformat() if best_at else None,
            "best_start_display": best.get("display"),
            "best_start_score": best.get("score"),
            "wake_window_start": wake_window_start.isoformat()
            if wake_window_start
            else None,
            "start_window_end": start_window_end.isoformat()
            if start_window_end
            else None,
            "wake_lead_minutes": wake_lead_minutes,
            "wake_interval_minutes": wake_interval_minutes,
            "start_grace_minutes": start_grace_minutes,
            "minimum_battery_percent": min_battery,
            "minimum_grass_growth_inches": min_growth,
            "next_sequence_growth_inches": next_growth if next_plan else None,
            "sequence_growth_candidate": growth_candidate,
            "minimum_mowing_favorability": min_favorability,
            "maximum_grass_wetness": max_wetness,
            "battery_percent": battery,
            "battery_status": charging_status,
            "battery_charging": charging,
            "charging_full_noise_battery_percent": CHARGING_FULL_NOISE_BATTERY_PERCENT,
            "mowing_favorability": favorability,
            "grass_wetness": wetness,
            "rtk_status": rtk,
            "head_type": head_type,
            "planning_state": planning,
            "recharging_state": recharging,
            "error_description": error_description,
            "last_auto_wake_at": self._last_auto_wake_at.get(sn),
            "last_auto_start_attempt_at": self._last_auto_start_attempt_at.get(sn),
            "last_auto_error": self._last_auto_error.get(sn),
        }

    async def _async_auto_sequence_check(self, _now=None) -> None:
        """Wake and optionally start the sequence near the best mow window."""
        changed = False
        for device in self.devices:
            sn = device.sn
            status = self.sequence_auto_ready_status(sn)
            self.auto_sequence_status[sn] = status
            changed = True

            if status["wake_due"]:
                await self._async_auto_wake(sn)
                changed = True

            if status["start_due"]:
                await self._async_update_weather_lookahead()
                status = self.sequence_auto_ready_status(sn)
                self.auto_sequence_status[sn] = status
                if status["start_due"]:
                    await self._async_auto_start(sn)
                    changed = True

        if changed:
            self.async_set_updated_data(self.data or {})

    async def _async_auto_wake(self, sn: str) -> None:
        """Wake the mower so final online and RTK checks can settle."""
        now = dt_util.now()
        self._last_auto_wake_at[sn] = now.isoformat()
        try:
            await self.async_set_working_state(sn, 1)
            self._last_auto_error[sn] = None
        except Exception as err:
            self._last_auto_error[sn] = f"wake failed: {err}"
            _LOGGER.warning("Automatic Yarbo wake failed for %s: %s", sn, err)

    async def _async_auto_start(self, sn: str) -> None:
        """Start the next queued sequence plan after all automation checks pass."""
        now = dt_util.now()
        self._last_auto_start_attempt_at[sn] = now.isoformat()
        try:
            await self.async_start_plan(sn, use_sequence=True)
            self._last_auto_error[sn] = None
        except Exception as err:
            self._last_auto_error[sn] = f"start failed: {err}"
            _LOGGER.warning("Automatic Yarbo sequence start failed for %s: %s", sn, err)

    def _field_int(self, sn: str, path: str) -> int | None:
        return _as_int(extract_field((self.data or {}).get(sn, {}), path))

    def _field_float(self, sn: str, path: str) -> float | None:
        return _as_float(extract_field((self.data or {}).get(sn, {}), path))

    def battery_charging(self, sn: str) -> bool | None:
        """Return filtered battery charging state."""
        status = self._field_int(sn, "BatteryMSG.status")
        if status is None:
            return None
        if status <= 1:
            return False

        battery = self._field_float(sn, "BatteryMSG.capacity")
        if battery is not None and battery >= CHARGING_FULL_NOISE_BATTERY_PERCENT:
            return False
        return True

    def _entity_state_float(
        self,
        sn: str,
        platform: str,
        key: str,
    ) -> float | None:
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{sn}_{key}")
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            return None
        return _as_float(state.state)

    def _minutes_elapsed_since(
        self,
        value: str | None,
        minutes: float,
        now: datetime,
    ) -> bool:
        parsed = self._parse_datetime(value)
        if parsed is None:
            return True
        return now - parsed >= timedelta(minutes=max(0.0, minutes))

    def _auto_start_grace_delta(self, sn: str) -> timedelta:
        minutes = self.auto_start_grace_minutes.get(
            sn, AUTO_START_GRACE_MINUTES_DEFAULT
        )
        return timedelta(minutes=max(0.0, minutes))

    def _mower_active_or_returning(self, sn: str) -> bool:
        planning = self._planning_state(sn)
        if planning is not None and planning > 0 and planning != COMPLETED_PLANNING_STATE:
            return True
        recharging = self._field_int(sn, "StateMSG.on_going_recharging")
        return (
            recharging is not None
            and recharging > 0
            and recharging != CHARGING_RECHARGE_STATE
        )

    async def async_core_command(self, sn: str, command: str) -> None:
        """Run a simple core command."""
        bound = self.bound_device(sn)
        if bound is None:
            raise ValueError("Yarbo device is unavailable")
        if command == "pause":
            await self.hass.async_add_executor_job(bound.core.pause)
        elif command == "resume":
            await self.hass.async_add_executor_job(bound.core.resume)
        elif command == "stop":
            await self.hass.async_add_executor_job(bound.core.stop)
        elif command == "dock":
            await self.hass.async_add_executor_job(bound.core.wireless_charging_cmd, 0)
            await self.hass.async_add_executor_job(bound.core.return_to_charge)
        else:
            raise ValueError(f"Unsupported command: {command}")

    async def async_set_blade_height(self, sn: str, height: int) -> None:
        """Set mower blade height."""
        bound = self.bound_device(sn)
        if bound is None:
            raise ValueError("Yarbo device is unavailable")
        await self.hass.async_add_executor_job(bound.mower.set_blade_height, height)

    async def async_set_blade_speed(self, sn: str, speed: int) -> None:
        """Set mower blade speed."""
        bound = self.bound_device(sn)
        if bound is None:
            raise ValueError("Yarbo device is unavailable")
        await self.hass.async_add_executor_job(bound.mower.set_blade_speed, speed)

    async def _async_initial_fetch(self) -> None:
        for device in self.devices:
            await self.async_refresh_device_msg(device.sn, device.type_id)
            await self.async_refresh_plans(device.sn, device.type_id)

    async def _async_restore_sequence(self) -> None:
        """Restore the locally managed plan sequence."""
        stored = await self._sequence_store.async_load()
        if not isinstance(stored, dict):
            return

        sequences = stored.get("plan_sequence") or {}
        if isinstance(sequences, dict):
            self.plan_sequence = {
                str(sn): [str(plan) for plan in plans if plan]
                for sn, plans in sequences.items()
                if isinstance(plans, list)
            }

        indexes = stored.get("sequence_index") or {}
        if isinstance(indexes, dict):
            self.sequence_index = {
                str(sn): max(0, int(index))
                for sn, index in indexes.items()
                if _as_int(index) is not None
            }

        pickers = stored.get("sequence_picker") or {}
        if isinstance(pickers, dict):
            self.sequence_picker = {
                str(sn): str(plan) if plan else None for sn, plan in pickers.items()
            }

        previous = stored.get("previous_completed_plan") or {}
        if isinstance(previous, dict):
            self.previous_completed_plan = {
                str(sn): str(plan) if plan else None for sn, plan in previous.items()
            }

        growth = stored.get("plan_growth_inches") or {}
        if isinstance(growth, dict):
            self.plan_growth_inches = {
                str(sn): {
                    str(plan): max(0.0, float(value))
                    for plan, value in plans.items()
                    if _as_float(value) is not None
                }
                for sn, plans in growth.items()
                if isinstance(plans, dict)
            }

        started = stored.get("plan_growth_started_at") or {}
        if isinstance(started, dict):
            self.plan_growth_started_at = {
                str(sn): {
                    str(plan): str(value)
                    for plan, value in plans.items()
                    if self._parse_datetime(value) is not None
                }
                for sn, plans in started.items()
                if isinstance(plans, dict)
            }

        last_mowed = stored.get("plan_last_mowed_at") or {}
        if isinstance(last_mowed, dict):
            self.plan_last_mowed_at = {
                str(sn): {
                    str(plan): str(value)
                    for plan, value in plans.items()
                    if self._parse_datetime(value) is not None
                }
                for sn, plans in last_mowed.items()
                if isinstance(plans, dict)
            }

        last_growth_update = stored.get("last_growth_update") or {}
        if isinstance(last_growth_update, dict):
            self._last_growth_update = {
                str(sn): str(value)
                for sn, value in last_growth_update.items()
                if self._parse_datetime(value) is not None
            }

        warm_weather_grass = stored.get("warm_weather_grass") or {}
        if isinstance(warm_weather_grass, dict):
            self.warm_weather_grass = {
                str(sn): bool(value) for sn, value in warm_weather_grass.items()
            }

        weather_source = stored.get("weather_source") or {}
        if isinstance(weather_source, dict):
            self.weather_source = {
                str(sn): str(value)
                for sn, value in weather_source.items()
                if isinstance(value, str) and value.startswith("weather.")
            }

    def _sequence_store_data(self) -> dict[str, Any]:
        return {
            "plan_sequence": self.plan_sequence,
            "sequence_index": self.sequence_index,
            "sequence_picker": self.sequence_picker,
            "previous_completed_plan": self.previous_completed_plan,
            "plan_growth_inches": self.plan_growth_inches,
            "plan_growth_started_at": self.plan_growth_started_at,
            "plan_last_mowed_at": self.plan_last_mowed_at,
            "last_growth_update": self._last_growth_update,
            "warm_weather_grass": self.warm_weather_grass,
            "weather_source": self.weather_source,
        }

    def _persist_sequence(self) -> None:
        self._sequence_store.async_delay_save(
            self._sequence_store_data, SEQUENCE_STORE_DELAY
        )

    def persist_local_state(self) -> None:
        """Persist local sequence and preference state."""
        self._persist_sequence()

    async def async_set_weather_source(self, sn: str, entity_id: str) -> None:
        """Set the HA weather entity used for mowing decisions."""
        options = self.weather_entity_options()
        if entity_id not in options:
            raise ValueError(f"Unknown weather entity: {entity_id}")
        self.weather_source[sn] = entity_id
        self._persist_sequence()
        self._refresh_weather_source_listener()
        await self._async_update_weather_lookahead()
        self.async_set_updated_data(self.data or {})

    def weather_entity_options(self) -> list[str]:
        """Return usable Home Assistant weather entities."""
        options = [
            state.entity_id
            for state in self.hass.states.async_all("weather")
            if state.entity_id.startswith("weather.")
        ]
        if WEATHER_ENTITY not in options:
            options.append(WEATHER_ENTITY)
        return self._sort_weather_entities(options)

    def plan_names(self, sn: str) -> list[str]:
        """Return locally cached Yarbo plan names."""
        names: list[str] = []
        for plan in self.plan_data.get(sn, []):
            name = plan.get("name")
            plan_id = plan.get("id")
            if name is None or plan_id is None:
                continue
            plan_name = str(name)
            if plan_name not in names:
                names.append(plan_name)
        return names

    def plan_id_by_name(self, sn: str, plan_name: str | None) -> int | None:
        """Return a plan id for a cached Yarbo plan name."""
        if plan_name is None:
            return None
        for plan in self.plan_data.get(sn, []):
            if str(plan.get("name")) != plan_name:
                continue
            try:
                return int(plan["id"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def plan_name_by_id(self, sn: str, plan_id: int | None) -> str | None:
        """Return a plan name for a cached Yarbo plan id."""
        if plan_id is None:
            return None
        for plan in self.plan_data.get(sn, []):
            try:
                candidate_id = int(plan["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if candidate_id == plan_id and plan.get("name") is not None:
                return str(plan["name"])
        return None

    def next_sequence_plan(self, sn: str) -> str | None:
        """Return the queued plan that will run next."""
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            return None
        return sequence[self._sequence_index_for(sn)]

    def next_run_plan(self, sn: str) -> str | None:
        """Return the plan used by the normal Start command."""
        return self.selected_plan_name.get(sn) or self.next_sequence_plan(sn)

    def sync_selected_plan_to_next(self, sn: str, *, force: bool = False) -> None:
        """Point the HA plan select at the next queued plan when appropriate."""
        selected_name = self.selected_plan_name.get(sn)
        if selected_name is not None:
            selected_id = self.plan_id_by_name(sn, selected_name)
            if selected_id is not None:
                self.selected_plan[sn] = selected_id
                if not force:
                    return

        next_plan = self.next_sequence_plan(sn)
        if next_plan is None:
            if selected_name is not None:
                self.selected_plan[sn] = self.plan_id_by_name(sn, selected_name)
            return

        plan_id = self.plan_id_by_name(sn, next_plan)
        if plan_id is None:
            return
        self.selected_plan_name[sn] = next_plan
        self.selected_plan[sn] = plan_id

    def weather_start_blocked(self) -> bool:
        """Return whether forecast weather should prevent starting a mow."""
        return bool(self.weather_lookahead.get("blocked"))

    def weather_start_block_reason(self) -> str:
        """Return a user-facing forecast block reason."""
        reason = self.weather_lookahead.get("reason") or "bad weather expected"
        return f"Cannot start: {reason}"

    def best_mow_start(self, sn: str) -> dict[str, Any]:
        """Return the best predicted mow start remaining today."""
        now = dt_util.now()
        daily = self.daily_best_mow_starts(sn, now=now)
        today_key = now.date().isoformat()
        today = next(
            (item for item in daily if item.get("date") == today_key),
            None,
        )
        if today is None:
            return {
                "status": "unknown",
                "start_at": None,
                "display": "Unknown",
                "score": None,
                "reason": "daily forecast unavailable",
                "candidate_count": 0,
                "daily_best_starts": daily,
            }

        result = dict(today)
        result["daily_best_starts"] = daily
        if result.get("status") == "ready":
            self._remember_best_mow_start(sn, result)
            return result

        held = self._held_best_mow_start(sn, result, daily, now)
        if held is not None:
            return held
        return result

    def _remember_best_mow_start(self, sn: str, result: dict[str, Any]) -> None:
        self._best_mow_start_hold[sn] = {
            key: value
            for key, value in result.items()
            if key != "daily_best_starts"
        }

    def _held_best_mow_start(
        self,
        sn: str,
        current: dict[str, Any],
        daily: list[dict[str, Any]],
        now: datetime,
    ) -> dict[str, Any] | None:
        if not self._can_hold_best_mow_start(current):
            return None

        held = self._best_mow_start_hold.get(sn)
        if held is None:
            return None

        start_at = self._parse_datetime(held.get("start_at"))
        if start_at is None or start_at.date() != now.date():
            self._best_mow_start_hold.pop(sn, None)
            return None

        hold_until = start_at + self._auto_start_grace_delta(sn)
        if now > hold_until:
            self._best_mow_start_hold.pop(sn, None)
            return None

        result = dict(held)
        base_reason = result.get("reason") or "selected best start"
        result["status"] = "ready"
        result["reason"] = f"{base_reason}; held through start grace window"
        result["held_after_forecast_rollover"] = True
        result["hold_until"] = hold_until.isoformat()
        result["daily_best_starts"] = self._daily_with_held_best_start(
            daily, result, now.date().isoformat()
        )
        return result

    def _can_hold_best_mow_start(self, current: dict[str, Any]) -> bool:
        if current.get("status") == "ready":
            return False
        if current.get("candidate_count", 0):
            return False
        if current.get("rejected_reasons"):
            return False
        return current.get("reason") in {
            "forecast hours are outside the daylight mowing window",
            "hourly forecast unavailable for this day",
        }

    def _daily_with_held_best_start(
        self,
        daily: list[dict[str, Any]],
        held: dict[str, Any],
        today_key: str,
    ) -> list[dict[str, Any]]:
        replacement_keys = {
            "status",
            "start_at",
            "display",
            "score",
            "reason",
            "temperature_f",
            "condition",
            "precipitation_probability",
            "precipitation",
            "wind_speed",
            "humidity",
            "dew_point_f",
            "dew_point_spread_f",
            "cloud_coverage",
            "held_after_forecast_rollover",
            "hold_until",
        }
        updated: list[dict[str, Any]] = []
        replaced = False
        for item in daily:
            if item.get("date") == today_key:
                merged = dict(item)
                for key in replacement_keys:
                    if key in held:
                        merged[key] = held[key]
                updated.append(merged)
                replaced = True
            else:
                updated.append(item)

        if not replaced:
            updated.insert(
                0,
                {
                    key: value
                    for key, value in held.items()
                    if key != "daily_best_starts"
                },
            )
        return updated

    def daily_best_mow_starts(
        self,
        sn: str,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return best mow start scoring for each available forecast day."""
        now = now or dt_util.now()
        horizon = now + timedelta(hours=BEST_MOW_START_LOOKAHEAD_HOURS)
        start_grace = self._auto_start_grace_delta(sn)
        candidate_start = now - start_grace
        sun = self.hass.states.get(SUN_ENTITY)
        sun_attrs = sun.attributes if sun is not None else {}
        sun_state = sun.state if sun is not None else None
        windows = self._daylight_windows(
            sn, candidate_start, horizon, sun_attrs, sun_state
        )
        forecast = self._forecast_window(self.weather_forecast, candidate_start, horizon)

        return [
            self._daily_best_mow_start(
                sn,
                now.date() + timedelta(days=offset),
                now,
                candidate_start,
                start_grace,
                forecast,
                windows,
            )
            for offset in range(BEST_MOW_START_FORECAST_DAYS)
        ]

    def _daily_best_mow_start(
        self,
        sn: str,
        day,
        now: datetime,
        candidate_start: datetime,
        start_grace: timedelta,
        forecast: list[dict[str, Any]],
        windows: list[tuple[datetime, datetime]],
    ) -> dict[str, Any]:
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo)
        day_end = day_start + timedelta(days=1)
        label = self._display_date_label(day, now)
        candidate_day_windows = [
            (max(window_start, day_start, candidate_start), min(window_end, day_end))
            for window_start, window_end in windows
            if window_end > day_start and window_start < day_end
        ]
        candidate_day_windows = [
            (window_start, window_end)
            for window_start, window_end in candidate_day_windows
            if window_end > window_start
        ]
        display_day_windows = [
            (max(window_start, day_start, now), min(window_end, day_end))
            for window_start, window_end in windows
            if window_end > day_start and window_start < day_end
        ]
        display_day_windows = [
            (window_start, window_end)
            for window_start, window_end in display_day_windows
            if window_end > window_start
        ]
        daylight_windows = [
            {
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            }
            for window_start, window_end in display_day_windows
        ]

        day_forecast = []
        outside_window_count = 0
        rejected_reasons: list[str] = []
        candidates: list[dict[str, Any]] = []
        for item in forecast:
            start = self._parse_datetime(item.get("datetime"))
            if start is None or start < day_start or start >= day_end:
                continue
            day_forecast.append(item)
            if not self._in_windows(start, candidate_day_windows):
                outside_window_count += 1
                continue

            reject_reason = self._best_mow_reject_reason(item)
            if reject_reason is not None:
                rejected_reasons.append(reject_reason)
                continue

            candidate = self._best_mow_candidate(item)
            if candidate is None:
                rejected_reasons.append("weather below mowing threshold")
                continue
            if candidate["score"] < BEST_MOW_START_MIN_SCORE:
                rejected_reasons.append(
                    f"score below {BEST_MOW_START_MIN_SCORE:g}"
                )
                continue
            candidates.append(candidate)

        candidates.sort(key=lambda candidate: candidate["score"], reverse=True)

        base: dict[str, Any] = {
            "date": day.isoformat(),
            "label": label,
            "minimum_drying_after_sunrise_hours": max(
                self.morning_blackout_hours.get(sn, 3.0),
                BEST_MOW_START_MIN_DRYING_HOURS,
            ),
            "minimum_score": BEST_MOW_START_MIN_SCORE,
            "candidate_grace_minutes": round(start_grace.total_seconds() / 60.0, 1),
            "forecast_count": len(day_forecast),
            "outside_daylight_window_count": outside_window_count,
            "candidate_count": len(candidates),
            "rejected_reasons": self._summarize_reasons(rejected_reasons),
            "candidates": candidates[:5],
            "daylight_windows": daylight_windows,
        }

        if candidates:
            best = candidates[0]
            return {
                **base,
                "status": "ready",
                "start_at": best["datetime"],
                "display": self._display_time(best["datetime"]),
                "score": best["score"],
                "reason": best["reason"],
                "temperature_f": best["temperature_f"],
                "condition": best["condition"],
                "precipitation_probability": best["precipitation_probability"],
                "precipitation": best["precipitation"],
                "wind_speed": best["wind_speed"],
                "humidity": best["humidity"],
                "dew_point_f": best["dew_point_f"],
                "dew_point_spread_f": best["dew_point_spread_f"],
                "cloud_coverage": best["cloud_coverage"],
            }

        if not candidate_day_windows:
            reason = (
                "no daylight mowing window remains today"
                if day == now.date()
                else "no daylight mowing window"
            )
            status = "no_candidate"
        elif not day_forecast:
            reason = "hourly forecast unavailable for this day"
            status = "unknown"
        else:
            reason = self._no_candidate_reason(rejected_reasons, outside_window_count)
            status = "no_candidate"

        return {
            **base,
            "status": status,
            "start_at": None,
            "display": "No candidate" if status == "no_candidate" else "Unknown",
            "score": None,
            "reason": reason,
            "temperature_f": None,
            "condition": None,
            "precipitation_probability": None,
            "precipitation": None,
            "wind_speed": None,
            "humidity": None,
            "dew_point_f": None,
            "dew_point_spread_f": None,
            "cloud_coverage": None,
        }

    def plan_growth_details(
        self, sn: str, plan_names: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return display-ready growth details for plans."""
        names = plan_names if plan_names is not None else self.plan_names(sn)
        return [
            self._plan_growth_detail(sn, plan_name, index + 1)
            for index, plan_name in enumerate(names)
        ]

    def set_sequence_picker(self, sn: str, plan_name: str) -> None:
        """Select a plan name for queue editing."""
        if plan_name not in self.plan_names(sn):
            raise ValueError(f"Unknown Yarbo plan: {plan_name}")
        self.sequence_picker[sn] = plan_name
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})

    def add_sequence_plan(self, sn: str) -> str:
        """Append the selected queue-editing plan."""
        plan_name = self._selected_sequence_plan(sn)
        if plan_name is None:
            raise ValueError("No Yarbo plan selected for the sequence")
        self.plan_sequence.setdefault(sn, []).append(plan_name)
        self._sort_sequence_by_growth(sn)
        self.sync_selected_plan_to_next(sn, force=True)
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})
        return plan_name

    def remove_sequence_plan(self, sn: str) -> str | None:
        """Remove the selected plan from the sequence, falling back to the last item."""
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            return None

        plan_name = self.sequence_picker.get(sn)
        remove_index = len(sequence) - 1
        if plan_name in sequence:
            remove_index = len(sequence) - 1 - sequence[::-1].index(plan_name)

        current_index = self._sequence_index_for(sn)
        removed = sequence.pop(remove_index)
        if not sequence:
            self.sequence_index[sn] = 0
        elif remove_index < current_index:
            self.sequence_index[sn] = (current_index - 1) % len(sequence)
        else:
            self.sequence_index[sn] = current_index % len(sequence)

        self.sync_selected_plan_to_next(sn, force=True)
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})
        return removed

    def advance_sequence_plan(self, sn: str) -> str | None:
        """Advance the sequence pointer to choose a different next plan."""
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            return None
        self.sequence_index[sn] = (self._sequence_index_for(sn) + 1) % len(sequence)
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})
        return sequence[self.sequence_index[sn]]

    def clear_sequence(self, sn: str) -> None:
        """Clear the local plan sequence."""
        self.plan_sequence[sn] = []
        self.sequence_index[sn] = 0
        self.sync_selected_plan_to_next(sn)
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})

    async def _async_refresh_stored_tokens(self) -> None:
        """Persist refreshed SDK tokens on the config entry."""
        if self._client is None:
            return
        token = getattr(self._client, "token", None)
        refresh_token = getattr(self._client, "refresh_token", None)
        if not token or not refresh_token:
            return
        if (
            token == self.entry.data.get(DATA_ACCESS_TOKEN)
            and refresh_token == self.entry.data.get(DATA_REFRESH_TOKEN)
        ):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                DATA_ACCESS_TOKEN: token,
                DATA_REFRESH_TOKEN: refresh_token,
            },
        )

    def _on_device_status(self, topic: str, data: dict[str, Any]) -> None:
        parts = topic.split("/")
        if len(parts) < 2:
            return
        sn = parts[1]
        self.data.setdefault(sn, {})
        if _deep_merge(self.data[sn], data):
            self._track_plan_transition(sn)
            self._track_error_code(sn)
            self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, self.data)

    def _on_heart_beat(self, topic: str, data: dict[str, Any]) -> None:
        parts = topic.split("/")
        if len(parts) < 2:
            return
        sn = parts[1]
        self._last_heartbeat[sn] = time.monotonic()
        self.data.setdefault(sn, {})
        was_online = self.data[sn].get("__online__")
        prev = self.data[sn].get("HeartBeatMSG")
        self.data[sn]["__online__"] = True
        self.data[sn]["HeartBeatMSG"] = data
        if was_online is not True or prev != data:
            self.hass.loop.call_soon_threadsafe(self.async_set_updated_data, self.data)

    async def _async_check_heartbeats(self, _now=None) -> None:
        now = time.monotonic()
        changed = False
        for device in self.devices:
            last = self._last_heartbeat.get(device.sn)
            if last is None or now - last > HEARTBEAT_TIMEOUT_SECONDS:
                self.data.setdefault(device.sn, {})
                if self.data[device.sn].get("__online__") is not False:
                    self.data[device.sn]["__online__"] = False
                    changed = True
        if changed:
            self.async_set_updated_data(self.data)

    async def _async_update_plan_growth(self, _now=None) -> None:
        """Accumulate estimated grass growth for each known plan."""
        now = dt_util.now()
        changed = False

        for device in self.devices:
            sn = device.sn
            growth_rate = self._growth_rate_inches_per_day(sn)
            self._ensure_plan_growth_entries(sn, now)

            last_update = self._parse_datetime(self._last_growth_update.get(sn))
            self._last_growth_update[sn] = now.isoformat()
            if last_update is None:
                changed = True
                continue

            elapsed_hours = (now - last_update).total_seconds() / 3600
            if elapsed_hours <= 0:
                continue

            elapsed_hours = min(elapsed_hours, GROWTH_MAX_CATCHUP_HOURS)
            increment = growth_rate * (elapsed_hours / 24)
            if increment <= 0:
                changed = True
                continue

            plan_growth = self.plan_growth_inches.setdefault(sn, {})
            for plan_name in self._known_growth_plan_names(sn):
                plan_growth[plan_name] = round(
                    max(0.0, plan_growth.get(plan_name, 0.0) + increment), 3
                )
                changed = True
            self._sort_sequence_by_growth(sn)

        if changed:
            self._persist_sequence()
            self.async_set_updated_data(self.data or {})

    async def _async_update_weather_lookahead(self, _now=None) -> None:
        """Fetch and score the next weather window."""
        lookahead = await self._async_weather_lookahead()
        if lookahead != self.weather_lookahead:
            self.weather_lookahead = lookahead
            self.async_set_updated_data(self.data or {})

    @callback
    def _async_weather_source_changed(self, _event: Event) -> None:
        self.entry.async_create_background_task(
            self.hass,
            self._async_update_weather_lookahead(),
            name=f"{DOMAIN}_weather_lookahead_state_changed",
        )

    def _refresh_weather_source_listener(self) -> None:
        if self._unsub_weather_state:
            self._unsub_weather_state()
        self._unsub_weather_state = async_track_state_change_event(
            self.hass,
            [self._weather_entity_id()],
            self._async_weather_source_changed,
        )

    async def _async_weather_lookahead(self) -> dict[str, Any]:
        weather_entity = self._weather_entity_id()
        now = dt_util.now()
        horizon = now + timedelta(hours=WEATHER_LOOKAHEAD_HOURS)
        weather = self.hass.states.get(weather_entity)
        attrs = weather.attributes if weather is not None else {}
        current_condition = weather.state if weather is not None else None

        base: dict[str, Any] = {
            "status": "clear",
            "blocked": False,
            "reason": "no bad weather expected",
            "weather_entity": weather_entity,
            "checked_at": now.isoformat(),
            "horizon_hours": WEATHER_LOOKAHEAD_HOURS,
            "horizon_until": horizon.isoformat(),
            "current_condition": current_condition,
            "forecast_available": False,
            "forecast_count": 0,
            "first_bad_weather_at": None,
            "first_bad_weather_condition": None,
            "forecast": [],
        }

        if weather is None or current_condition in UNAVAILABLE_STATES:
            base.update(
                {
                    "status": "unknown",
                    "reason": f"{weather_entity} is unavailable",
                }
            )
            return base

        current_reason = self._current_weather_block_reason(current_condition, attrs)

        if not self.hass.services.has_service("weather", "get_forecasts"):
            if current_reason is not None:
                base.update(
                    {
                        "status": "blocked",
                        "blocked": True,
                        "reason": current_reason,
                        "first_bad_weather_at": now.isoformat(),
                        "first_bad_weather_condition": current_condition,
                    }
                )
            else:
                base.update(
                    {
                        "status": "unknown",
                        "reason": "weather forecast service unavailable",
                    }
                )
            return base

        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                blocking=True,
                return_response=True,
                target={"entity_id": weather_entity},
            )
        except Exception as err:
            _LOGGER.debug("Weather forecast lookahead failed: %s", err)
            base.update(
                {
                    "status": "unknown",
                    "reason": f"forecast unavailable: {err}",
                }
            )
            return base

        forecast = self._extract_forecast(response, weather_entity)
        considered = self._forecast_window(forecast, now, horizon)
        self.weather_forecast = self._forecast_window(
            forecast, now, now + timedelta(hours=BEST_MOW_START_LOOKAHEAD_HOURS)
        )
        base["forecast_available"] = bool(forecast)
        base["forecast_count"] = len(considered)
        base["forecast"] = considered

        if current_reason is not None:
            base.update(
                {
                    "status": "blocked",
                    "blocked": True,
                    "reason": current_reason,
                    "first_bad_weather_at": now.isoformat(),
                    "first_bad_weather_condition": current_condition,
                }
            )
            return base

        for item in considered:
            reason = self._forecast_block_reason(item)
            if reason is None:
                continue
            base.update(
                {
                    "status": "blocked",
                    "blocked": True,
                    "reason": reason,
                    "first_bad_weather_at": item.get("datetime"),
                    "first_bad_weather_condition": item.get("condition"),
                }
            )
            return base

        if not forecast:
            base.update(
                {
                    "status": "unknown",
                    "reason": "hourly forecast unavailable",
                }
            )
        return base

    def _extract_forecast(
        self, response: Any, weather_entity: str
    ) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []

        entity_response = response.get(weather_entity)
        if entity_response is None and len(response) == 1:
            entity_response = next(iter(response.values()))

        forecast = None
        if isinstance(entity_response, dict):
            forecast = entity_response.get("forecast")
        elif isinstance(entity_response, list):
            forecast = entity_response
        elif isinstance(response.get("forecast"), list):
            forecast = response["forecast"]

        if not isinstance(forecast, list):
            return []
        return [item for item in forecast if isinstance(item, dict)]

    def _forecast_window(
        self,
        forecast: list[dict[str, Any]],
        now: datetime,
        horizon: datetime,
    ) -> list[dict[str, Any]]:
        window: list[dict[str, Any]] = []
        undated_limit = WEATHER_LOOKAHEAD_HOURS + 1

        for index, item in enumerate(forecast):
            raw_dt = item.get("datetime")
            parsed = self._parse_datetime(raw_dt)
            if parsed is None:
                if index >= undated_limit:
                    continue
            elif parsed < now - timedelta(minutes=10) or parsed > horizon:
                continue

            window.append(
                {
                    "datetime": parsed.isoformat() if parsed is not None else raw_dt,
                    "condition": item.get("condition"),
                    "precipitation_probability": self._float_value(
                        item.get("precipitation_probability")
                    ),
                    "precipitation": self._float_value(item.get("precipitation")),
                    "wind_speed": self._float_value(item.get("wind_speed")),
                    "temperature": self._temperature_f(item.get("temperature")),
                    "humidity": self._float_value(item.get("humidity")),
                    "dew_point": self._temperature_f(item.get("dew_point")),
                    "cloud_coverage": self._float_value(item.get("cloud_coverage")),
                }
            )
        return window

    def _current_weather_block_reason(
        self, condition: str | None, attrs: dict[str, Any]
    ) -> str | None:
        if condition in START_BLOCK_WEATHER:
            return f"current weather is {condition}"

        wind_speed = self._float_value(attrs.get("wind_speed"))
        if wind_speed is not None and wind_speed >= WEATHER_LOOKAHEAD_WIND_SPEED:
            return f"current wind is {wind_speed:g}"

        return None

    def _forecast_block_reason(self, item: dict[str, Any]) -> str | None:
        when = self._forecast_time_label(item.get("datetime"))
        condition = item.get("condition")
        if condition in START_BLOCK_WEATHER:
            return f"{condition} expected {when}"

        probability = self._float_value(item.get("precipitation_probability"))
        if (
            probability is not None
            and probability >= WEATHER_LOOKAHEAD_PRECIP_PROBABILITY
        ):
            return f"{probability:g}% precipitation chance {when}"

        precipitation = self._float_value(item.get("precipitation"))
        if precipitation is not None and precipitation >= WEATHER_LOOKAHEAD_PRECIP_AMOUNT:
            return f"{precipitation:g} precipitation forecast {when}"

        wind_speed = self._float_value(item.get("wind_speed"))
        if wind_speed is not None and wind_speed >= WEATHER_LOOKAHEAD_WIND_SPEED:
            return f"{wind_speed:g} wind forecast {when}"

        return None

    def _daylight_windows(
        self,
        sn: str,
        now: datetime,
        horizon: datetime,
        sun_attrs: dict[str, Any],
        sun_state: str | None,
    ) -> list[tuple[datetime, datetime]]:
        next_rising = self._parse_datetime(sun_attrs.get("next_rising"))
        next_setting = self._parse_datetime(sun_attrs.get("next_setting"))
        if next_rising is None or next_setting is None:
            return []

        windows: list[tuple[datetime, datetime]] = []
        morning_blackout = timedelta(
            hours=max(
                self.morning_blackout_hours.get(sn, 3.0),
                BEST_MOW_START_MIN_DRYING_HOURS,
            )
        )
        evening_blackout = timedelta(
            hours=self.evening_blackout_hours.get(sn, 3.0)
        )

        if sun_state == "above_horizon":
            rising = next_rising - timedelta(days=1)
            setting = next_setting
        else:
            rising = next_rising
            setting = next_setting
            if setting <= rising:
                setting += timedelta(days=1)

        for day_offset in range(BEST_MOW_START_FORECAST_DAYS):
            start = rising + timedelta(days=day_offset) + morning_blackout
            end = setting + timedelta(days=day_offset) - evening_blackout
            if end <= start:
                continue
            start = max(start, now)
            end = min(end, horizon)
            if end > start:
                windows.append((start, end))
        return windows

    def _in_windows(
        self, value: datetime, windows: list[tuple[datetime, datetime]]
    ) -> bool:
        return any(start <= value <= end for start, end in windows)

    def _best_mow_reject_reason(self, item: dict[str, Any]) -> str | None:
        condition = item.get("condition")
        if condition in START_BLOCK_WEATHER:
            return f"{condition} forecast"

        probability = self._float_value(item.get("precipitation_probability"))
        precipitation = self._float_value(item.get("precipitation"))
        wind_speed = self._float_value(item.get("wind_speed"))
        temperature = self._float_value(item.get("temperature"))
        humidity = self._float_value(item.get("humidity"))
        dew_point = self._float_value(item.get("dew_point"))
        dew_spread = (
            temperature - dew_point
            if temperature is not None and dew_point is not None
            else None
        )

        if precipitation is not None and precipitation >= WEATHER_LOOKAHEAD_PRECIP_AMOUNT:
            return f"{precipitation:g} precipitation forecast"
        if (
            probability is not None
            and probability >= WEATHER_LOOKAHEAD_PRECIP_PROBABILITY
        ):
            return f"{probability:g}% precipitation chance"
        if wind_speed is not None and wind_speed >= WEATHER_LOOKAHEAD_WIND_SPEED:
            return f"{wind_speed:g} wind forecast"
        if dew_spread is not None and dew_spread <= 2.0:
            return "dew likely"
        if humidity is not None and humidity >= 97.0:
            return "humidity too high"

        return None

    def _best_mow_candidate(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if self._best_mow_reject_reason(item) is not None:
            return None

        condition = item.get("condition")
        probability = self._float_value(item.get("precipitation_probability"))
        precipitation = self._float_value(item.get("precipitation"))
        wind_speed = self._float_value(item.get("wind_speed"))
        temperature = self._float_value(item.get("temperature"))
        humidity = self._float_value(item.get("humidity"))
        dew_point = self._float_value(item.get("dew_point"))
        cloud_coverage = self._float_value(item.get("cloud_coverage"))
        dew_spread = (
            temperature - dew_point
            if temperature is not None and dew_point is not None
            else None
        )

        dry_score = 100.0
        if probability is not None:
            dry_score -= min(70.0, probability * 1.25)
        if precipitation is not None:
            dry_score -= min(80.0, precipitation * 300.0)
        if humidity is not None:
            if humidity >= 90:
                dry_score -= min(35.0, (humidity - 85.0) * 2.0)
            elif humidity >= 80:
                dry_score -= 8.0
        if dew_spread is not None:
            if dew_spread <= 4:
                dry_score -= 35.0
            elif dew_spread <= 7:
                dry_score -= 20.0
            elif dew_spread <= 10:
                dry_score -= 8.0
        if cloud_coverage is not None and cloud_coverage >= 85:
            dry_score -= 10.0
        if condition in DULL_WEATHER:
            dry_score -= 12.0
        elif condition in SUNNY_WEATHER:
            dry_score += 8.0

        if temperature is None:
            cool_score = 55.0
        else:
            cool_score = 100.0 - abs(temperature - BEST_MOW_START_TARGET_TEMP_F) * 3.0
            if temperature >= 85:
                cool_score -= (temperature - 85) * 3.0
            elif temperature <= 45:
                cool_score -= (45 - temperature) * 2.0

        if cloud_coverage is not None:
            sun_score = self._clamp_float(100.0 - cloud_coverage * 0.55)
        else:
            sun_score = 75.0
            if condition == "sunny":
                sun_score = 100.0
            elif condition in {"partlycloudy", "partly-cloudy"}:
                sun_score = 88.0
            elif condition == "cloudy":
                sun_score = 52.0
            elif condition == "fog":
                sun_score = 35.0

        wind_score = 90.0
        if wind_speed is not None:
            wind_score = 100.0 - max(0.0, wind_speed - 5.0) * 3.0

        score = self._clamp_float(
            dry_score * 0.50 + cool_score * 0.25 + sun_score * 0.18 + wind_score * 0.07
        )
        return {
            "datetime": item.get("datetime"),
            "display": self._display_time(item.get("datetime")),
            "score": round(score, 1),
            "reason": self._best_mow_reason(
                condition,
                temperature,
                probability,
                precipitation,
                wind_speed,
                humidity,
                dew_spread,
            ),
            "condition": condition,
            "temperature_f": temperature,
            "precipitation_probability": probability,
            "precipitation": precipitation,
            "wind_speed": wind_speed,
            "humidity": humidity,
            "dew_point_f": dew_point,
            "dew_point_spread_f": round(dew_spread, 1)
            if dew_spread is not None
            else None,
            "cloud_coverage": cloud_coverage,
        }

    def _best_mow_reason(
        self,
        condition: str | None,
        temperature: float | None,
        probability: float | None,
        precipitation: float | None,
        wind_speed: float | None,
        humidity: float | None,
        dew_spread: float | None,
    ) -> str:
        parts: list[str] = []
        if condition:
            parts.append(str(condition))
        if temperature is not None:
            parts.append(f"{temperature:g} F")
        if humidity is not None:
            parts.append(f"{humidity:g}% humidity")
        if dew_spread is not None and dew_spread <= 10:
            parts.append(f"{dew_spread:g} F dew spread")
        if probability is not None:
            parts.append(f"{probability:g}% precip")
        if precipitation is not None and precipitation > 0:
            parts.append(f"{precipitation:g} precip")
        if wind_speed is not None:
            parts.append(f"{wind_speed:g} wind")
        return ", ".join(parts) or "best dry/cool daylight window"

    def _summarize_reasons(self, reasons: list[str]) -> list[str]:
        counts: dict[str, int] = {}
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
        return [
            f"{reason} ({count})" if count > 1 else reason
            for reason, count in counts.items()
        ]

    def _no_candidate_reason(
        self,
        rejected_reasons: list[str],
        outside_window_count: int,
    ) -> str:
        summary = self._summarize_reasons(rejected_reasons)
        if summary:
            return f"no acceptable forecast hour: {', '.join(summary[:3])}"
        if outside_window_count:
            return "forecast hours are outside the daylight mowing window"
        return "no acceptable mowing forecast remains"

    def _display_date_label(self, day, now: datetime) -> str:
        today = now.date()
        if day == today:
            return "Today"
        if day == today + timedelta(days=1):
            return "Tomorrow"
        return day.strftime("%a %-m/%-d")

    def _display_time(self, value: Any) -> str:
        parsed = self._parse_datetime(value)
        if parsed is None:
            return "Unknown"
        return parsed.strftime("%-I:%M %p")

    def _clamp_float(self, value: float) -> float:
        return max(0.0, min(100.0, value))

    def _forecast_time_label(self, value: Any) -> str:
        parsed = self._parse_datetime(value)
        if parsed is None:
            return "within the next 3 hours"
        minutes = round((parsed - dt_util.now()).total_seconds() / 60)
        if minutes <= 0:
            return "now"
        if minutes < 90:
            return f"in {minutes} minutes"
        return f"in {round(minutes / 60, 1):g} hours"

    def _ensure_sequence_picker(self, sn: str) -> None:
        if self.sequence_picker.get(sn) in self.plan_names(sn):
            return
        selected = self.selected_plan_name.get(sn)
        if selected in self.plan_names(sn):
            self.sequence_picker[sn] = selected
            return
        plans = self.plan_names(sn)
        self.sequence_picker[sn] = plans[0] if plans else None

    def _selected_sequence_plan(self, sn: str) -> str | None:
        self._ensure_sequence_picker(sn)
        plan_name = self.sequence_picker.get(sn)
        return plan_name if plan_name in self.plan_names(sn) else None

    def _known_growth_plan_names(self, sn: str) -> list[str]:
        names: list[str] = []
        for plan_name in (
            self.plan_names(sn)
            + self.plan_sequence.get(sn, [])
            + list(self.plan_growth_inches.get(sn, {}))
        ):
            if plan_name not in names:
                names.append(plan_name)
        return names

    def _ensure_plan_growth_entries(
        self, sn: str, now: datetime | None = None
    ) -> None:
        now = now or dt_util.now()
        now_iso = now.isoformat()
        plan_growth = self.plan_growth_inches.setdefault(sn, {})
        plan_started = self.plan_growth_started_at.setdefault(sn, {})
        for plan_name in self._known_growth_plan_names(sn):
            plan_growth.setdefault(plan_name, 0.0)
            plan_started.setdefault(plan_name, now_iso)

    def _reset_plan_growth(self, sn: str, plan_name: str) -> None:
        now_iso = dt_util.now().isoformat()
        self.plan_growth_inches.setdefault(sn, {})[plan_name] = 0.0
        self.plan_growth_started_at.setdefault(sn, {})[plan_name] = now_iso
        self.plan_last_mowed_at.setdefault(sn, {})[plan_name] = now_iso

    def _plan_growth_value(self, sn: str, plan_name: str | None) -> float:
        if plan_name is None:
            return 0.0
        return max(0.0, self.plan_growth_inches.get(sn, {}).get(plan_name, 0.0))

    def _sort_sequence_by_growth(
        self,
        sn: str,
        *,
        demote_plan: str | None = None,
    ) -> bool:
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            self.sequence_index[sn] = 0
            return False

        current_index = self._sequence_index_for(sn)
        old_sequence = list(sequence)

        growth = self.plan_growth_inches.setdefault(sn, {})
        sorted_sequence = [
            plan_name
            for original_index, plan_name in sorted(
                enumerate(old_sequence),
                key=lambda item: (
                    -growth.get(item[1], 0.0),
                    item[1] == demote_plan,
                    item[0],
                ),
            )
        ]

        self.plan_sequence[sn] = sorted_sequence
        self.sequence_index[sn] = 0

        return sorted_sequence != old_sequence or current_index != 0

    def _plan_growth_detail(
        self, sn: str, plan_name: str, position: int | None = None
    ) -> dict[str, Any]:
        started_at = self.plan_growth_started_at.get(sn, {}).get(plan_name)
        last_mowed_at = self.plan_last_mowed_at.get(sn, {}).get(plan_name)
        started = self._parse_datetime(started_at)
        growth_days = None
        if started is not None:
            growth_days = round(
                max(0.0, (dt_util.now() - started).total_seconds() / 86400), 1
            )
        return {
            "position": position,
            "name": plan_name,
            "growth_since_last_mow_in": round(
                self.plan_growth_inches.get(sn, {}).get(plan_name, 0.0), 2
            ),
            "growth_days": growth_days,
            "growth_started_at": started_at,
            "last_mowed_at": last_mowed_at,
        }

    def _sequence_index_for(self, sn: str) -> int:
        sequence = self.plan_sequence.get(sn, [])
        if not sequence:
            return 0
        index = self.sequence_index.get(sn, 0)
        return max(0, index) % len(sequence)

    def _planning_state(self, sn: str) -> int | None:
        return _as_int(extract_field((self.data or {}).get(sn, {}), "StateMSG.on_going_planning"))

    def _track_plan_transition(self, sn: str) -> None:
        state = self._planning_state(sn)
        if state is None:
            return
        previous = self._last_planning_state.get(sn)
        self._last_planning_state[sn] = state
        if state != COMPLETED_PLANNING_STATE or previous == COMPLETED_PLANNING_STATE:
            return
        if previous not in ACTIVE_PLANNING_STATES and not self.active_plan_name.get(sn):
            return
        self.hass.loop.call_soon_threadsafe(self._mark_plan_completed, sn)

    def _track_error_code(self, sn: str) -> None:
        error_code = self._field_int(sn, "StateMSG.error_code")
        previous = self._last_error_code.get(sn)
        if error_code == previous:
            return

        self._last_error_code[sn] = error_code
        self.hass.loop.call_soon_threadsafe(
            self._update_error_notification,
            sn,
            error_code,
        )

    def _update_error_notification(self, sn: str, error_code: int | None) -> None:
        notification_id = f"{DOMAIN}_{sn}_error_code"
        if error_code in (None, 0):
            persistent_notification.async_dismiss(self.hass, notification_id)
            return

        description = describe_error_code(error_code) or f"Unknown Yarbo error {error_code}"
        device = self.device_by_sn(sn)
        device_name = getattr(device, "name", None) or getattr(device, "sn", sn)
        persistent_notification.async_create(
            self.hass,
            (
                f"{device_name} reported error code {error_code}: {description}.\n\n"
                "Check the mower and Yarbo app before starting or resuming mowing."
            ),
            title=f"{APP_NAME} error {error_code}",
            notification_id=notification_id,
        )

    def _mark_plan_started(
        self, sn: str, plan_name: str | None, sequence_start: bool
    ) -> None:
        self.active_plan_name[sn] = plan_name or UNKNOWN_PLAN
        self.active_sequence_plan[sn] = sequence_start
        self._last_planning_state[sn] = self._planning_state(sn)
        if plan_name and plan_name != UNKNOWN_PLAN:
            self._reset_plan_growth(sn, plan_name)
            self._sort_sequence_by_growth(sn, demote_plan=plan_name)
            self.sync_selected_plan_to_next(sn, force=True)
            self._persist_sequence()
        self.async_set_updated_data(self.data or {})

    def _mark_plan_completed(self, sn: str) -> None:
        plan_name = self.active_plan_name.pop(sn, None)
        if not plan_name:
            return

        self.previous_completed_plan[sn] = plan_name
        was_sequence_plan = self.active_sequence_plan.pop(sn, False)

        self.sync_selected_plan_to_next(sn, force=was_sequence_plan)
        self._persist_sequence()
        self.async_set_updated_data(self.data or {})

    def growth_weather_metrics(self, sn: str | None = None) -> dict[str, Any]:
        """Return the current grass-growth model inputs and output."""
        metrics = self._growth_weather_inputs(sn)
        profile = self._growth_profile(sn)
        metrics["growth_rate_inches_per_day"] = self._growth_rate_inches_per_day(
            sn, metrics
        )
        metrics["grass_profile"] = (
            "warm_weather" if self._warm_weather_grass_enabled(sn) else "cold_weather"
        )
        metrics["grass_profile_label"] = profile["label"]
        metrics["growth_model"] = profile["model"]
        metrics["max_daily_growth_inches"] = profile["max_daily_inches"]
        metrics["growth_optimal_temp_f"] = profile["optimal_temp_f"]
        metrics["growth_temp_spread_f"] = profile["temp_spread_f"]
        metrics["growth_minimum_temp_f"] = profile["minimum_temp_f"]
        metrics["growth_maximum_temp_f"] = profile["maximum_temp_f"]
        return metrics

    def _growth_rate_inches_per_day(
        self, sn: str | None = None, metrics: dict[str, Any] | None = None
    ) -> float:
        metrics = metrics or self._growth_weather_inputs(sn)
        profile = self._growth_profile(sn)
        temp_f = metrics["temperature_f"]
        humidity = metrics["humidity"]
        cloud_coverage = metrics["cloud_coverage"]
        condition = metrics["condition"]
        sun_state = metrics["sun_state"]

        if temp_f is None:
            temp_factor = 0.35
        else:
            temp_factor = math.exp(
                -0.5
                * ((temp_f - profile["optimal_temp_f"]) / profile["temp_spread_f"])
                ** 2
            )
            if (
                temp_f < profile["minimum_temp_f"]
                or temp_f > profile["maximum_temp_f"]
            ):
                temp_factor = 0.0

        moisture_factor = 1.0
        if humidity is not None:
            if humidity < 25:
                moisture_factor = 0.55
            elif humidity < 40:
                moisture_factor = 0.75
            elif humidity > 96:
                moisture_factor = 0.85

        sunlight_factor = 0.9
        if cloud_coverage is not None:
            if cloud_coverage >= 90:
                sunlight_factor = 0.65
            elif cloud_coverage >= 70:
                sunlight_factor = 0.8
            elif cloud_coverage <= 30:
                sunlight_factor = 1.0
        if sun_state == "below_horizon":
            sunlight_factor *= 0.75

        weather_factor = 0.65 if condition in BAD_GROWTH_WEATHER else 1.0
        return round(
            profile["max_daily_inches"]
            * temp_factor
            * moisture_factor
            * sunlight_factor
            * weather_factor,
            4,
        )

    def _warm_weather_grass_enabled(self, sn: str | None) -> bool:
        return bool(sn is not None and self.warm_weather_grass.get(sn, False))

    def _growth_profile(self, sn: str | None) -> dict[str, Any]:
        if self._warm_weather_grass_enabled(sn):
            return GROWTH_PROFILES["warm_weather"]
        return GROWTH_PROFILES["cold_weather"]

    def _growth_weather_inputs(self, sn: str | None = None) -> dict[str, Any]:
        weather_entity = self._weather_entity_id(sn)
        weather = self.hass.states.get(weather_entity)
        sun = self.hass.states.get(SUN_ENTITY)
        attrs = weather.attributes if weather is not None else {}

        return {
            "weather_entity": weather_entity,
            "condition": weather.state if weather is not None else None,
            "temperature_f": self._temperature_f(attrs.get("temperature")),
            "humidity": self._float_value(attrs.get("humidity")),
            "cloud_coverage": self._float_value(attrs.get("cloud_coverage")),
            "sun_state": sun.state if sun is not None else None,
        }

    def weather_entity_id(self, sn: str | None = None) -> str:
        """Return the weather entity selected for Yarbo decisions."""
        selected = self.weather_source.get(sn) if sn is not None else None
        if selected is None:
            for device in self.devices:
                selected = self.weather_source.get(device.sn)
                if selected is not None:
                    break
        if selected in self.weather_entity_options():
            return selected

        preferred_weather = self._preferred_weather_entity()
        if preferred_weather is not None:
            return preferred_weather

        preferred = self.hass.states.get(WEATHER_ENTITY)
        if preferred is not None and preferred.state not in UNAVAILABLE_STATES:
            return WEATHER_ENTITY

        weather_states = [
            state
            for state in self.hass.states.async_all("weather")
            if state.state not in UNAVAILABLE_STATES
        ]
        if weather_states:
            return weather_states[0].entity_id

        if preferred is not None:
            return WEATHER_ENTITY

        weather_states = self.hass.states.async_all("weather")
        if weather_states:
            return weather_states[0].entity_id
        return WEATHER_ENTITY

    def _weather_entity_id(self, sn: str | None = None) -> str:
        return self.weather_entity_id(sn)

    def _preferred_weather_entity(self) -> str | None:
        """Return the best available preferred weather provider entity."""
        for entity_id in self.weather_entity_options():
            if self._weather_entity_platform(entity_id) not in PREFERRED_WEATHER_PLATFORMS:
                continue
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in UNAVAILABLE_STATES:
                return entity_id
        return None

    def _sort_weather_entities(self, entity_ids: list[str]) -> list[str]:
        """Sort weather entities with preferred providers first."""
        return sorted(
            set(entity_ids),
            key=lambda entity_id: (
                self._weather_entity_platform(entity_id)
                not in PREFERRED_WEATHER_PLATFORMS,
                entity_id != WEATHER_ENTITY,
                entity_id,
            ),
        )

    def _weather_entity_platform(self, entity_id: str) -> str | None:
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        return entry.platform if entry is not None else None

    def _temperature_f(self, value: Any) -> float | None:
        raw = self._float_value(value)
        if raw is None:
            return None
        unit = str(self.hass.config.units.temperature_unit)
        if unit in {"°C", "C"}:
            return round(raw * 9 / 5 + 32, 1)
        return round(raw, 1)

    def _float_value(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _parse_datetime(self, value: Any) -> datetime | None:
        if value is None:
            return None
        parsed = dt_util.parse_datetime(str(value))
        if parsed is None:
            return None
        return dt_util.as_local(parsed)
